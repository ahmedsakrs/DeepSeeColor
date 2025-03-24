# Copyright 2023 Stewart Jamieson, Woods Hole Oceanographic Institution
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License, version 3, as published by the Free Software Foundation.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License along with this program. If not, see <https://www.gnu.org/licenses/>. 

import os
import argparse
import numpy as np

import torch
from time import time
from torch.utils.data import DataLoader, Dataset
import torch.nn as nn
import torchvision.transforms as transforms
from torchvision.utils import save_image
import kornia.morphology as morph
from PIL import Image
try:
    from tqdm import trange
except:
    trange = range

class PairedRGBDepthDataset(Dataset):
    def __init__(self, image_path, depth_path, openni_depth, mask_max_depth, image_height, image_width, device):
        self.image_dir = image_path
        self.depth_dir = depth_path
        self.image_files = sorted(os.listdir(image_path))
        self.depth_files = sorted(os.listdir(depth_path))
        self.device = device
        self.openni_depth = openni_depth
        self.mask_max_depth = mask_max_depth
        self.crop = (0, 0, image_height, image_width)
        self.depth_perc = 0.0001
        self.kernel = torch.ones(3, 3).to(device=device)
        self.image_transforms = transforms.Compose([
            transforms.Resize((self.crop[2], self.crop[3]), transforms.InterpolationMode.BILINEAR, antialias=True),
            transforms.PILToTensor(),
        ])
        assert len(self.image_files) == len(self.depth_files)

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, index):
        fname = self.image_files[index]
        image = Image.open(os.path.join(self.image_dir, fname))
        depth_fname = self.depth_files[index]
        depth = Image.open(os.path.join(self.depth_dir, depth_fname))
        depth_transformed: torch.Tensor = self.image_transforms(depth).float().to(device=self.device)
        if self.openni_depth:
            depth_transformed = depth_transformed / 1000.
        if self.mask_max_depth:
            depth_transformed[depth_transformed == 0.] = depth_transformed.max()
        low, high = torch.nanquantile(depth_transformed, self.depth_perc), torch.nanquantile(depth_transformed,
                                                                                             1. - self.depth_perc)
        depth_transformed[(depth_transformed < low) | (depth_transformed > high)] = 0.
        depth_transformed = torch.squeeze(morph.closing(torch.unsqueeze(depth_transformed, dim=0), self.kernel), dim=0)
        left_transformed: torch.Tensor = self.image_transforms(image).to(device=self.device) / 255.
        return left_transformed, depth_transformed, [fname]


class BackscatterNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.backscatter_conv = nn.Conv2d(1, 3, 1, bias=False)
        self.residual_conv = nn.Conv2d(1, 3, 1, bias=False)
        nn.init.uniform_(self.backscatter_conv.weight, 0, 5)
        nn.init.uniform_(self.residual_conv.weight, 0, 5)
        self.B_inf = nn.Parameter(torch.rand(3, 1, 1))
        self.J_prime = nn.Parameter(torch.rand(3, 1, 1))
        self.sigmoid = nn.Sigmoid()
        self.relu = nn.ReLU()

    def forward(self, image, depth):
        beta_b_conv = self.relu(self.backscatter_conv(depth))
        beta_d_conv = self.relu(self.residual_conv(depth))
        Bc = self.B_inf * (1 - torch.exp(-beta_b_conv)) + self.J_prime * torch.exp(-beta_d_conv)
        backscatter = self.sigmoid(Bc)
        backscatter_masked = backscatter * (depth > 0.).repeat(1, 3, 1, 1)
        direct = image - backscatter_masked
        return direct, backscatter


class DeattenuateNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.attenuation_conv = nn.Conv2d(1, 6, 1, bias=False)
        nn.init.uniform_(self.attenuation_conv.weight, 0, 5)
        self.attenuation_coef = nn.Parameter(torch.rand(6, 1, 1))
        self.relu = nn.ReLU()
        self.wb = nn.Parameter(torch.rand(1, 1, 1))
        nn.init.constant_(self.wb, 1)
        self.output_act = nn.Sigmoid()

    def forward(self, direct, depth):
        attn_conv = torch.exp(-self.relu(self.attenuation_conv(depth)))
        beta_d = torch.stack(tuple(
            torch.sum(attn_conv[:, i:i + 2, :, :] * self.relu(self.attenuation_coef[i:i + 2]), dim=1) for i in
            range(0, 6, 2)), dim=1)
        f = torch.exp(torch.clamp(beta_d * depth, 0, float(torch.log(torch.tensor([3.])))))
        f_masked = f * ((depth == 0.) / f + (depth > 0.))
        J = f_masked * direct * self.wb
        nanmask = torch.isnan(J)
        if torch.any(nanmask):
            print("Warning! NaN values in J")
            J[nanmask] = 0
        return f_masked, J


class BackscatterLoss(nn.Module):
    def __init__(self, cost_ratio=1000.):
        super().__init__()
        self.l1 = nn.L1Loss()
        self.smooth_l1 = nn.SmoothL1Loss(beta=0.2)
        self.mse = nn.MSELoss()
        self.relu = nn.ReLU()
        self.cost_ratio = cost_ratio

    def forward(self, direct):
        pos = self.l1(self.relu(direct), torch.zeros_like(direct))
        neg = self.smooth_l1(self.relu(-direct), torch.zeros_like(direct))
        bs_loss = self.cost_ratio * neg + pos
        return bs_loss


class DeattenuateLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.mse = nn.MSELoss()
        self.relu = nn.ReLU()
        self.target_intensity = 0.5

    def forward(self, direct, J):
        saturation_loss = (self.relu(-J) + self.relu(J - 1)).square().mean()
        init_spatial = torch.std(direct, dim=[2, 3])
        channel_intensities = torch.mean(J, dim=[2, 3], keepdim=True)
        channel_spatial = torch.std(J, dim=[2, 3])
        intensity_loss = (channel_intensities - self.target_intensity).square().mean()
        spatial_variation_loss = self.mse(channel_spatial, init_spatial)
        if torch.any(torch.isnan(saturation_loss)):
            print("NaN saturation loss!")
        if torch.any(torch.isnan(intensity_loss)):
            print("NaN intensity loss!")
        if torch.any(torch.isnan(spatial_variation_loss)):
            print("NaN spatial variation loss!")
        return saturation_loss + intensity_loss + spatial_variation_loss


def main(args):
    train_dataset = PairedRGBDepthDataset(args.images, args.depth, args.depth_16u, args.mask_max_depth, args.height,
                                             args.width, args.device)
    save_dir = args.output
    os.makedirs(save_dir, exist_ok=True)
    target_batch_size = 1
    dataloader = DataLoader(train_dataset, batch_size=target_batch_size, shuffle=False)
    bs_model = BackscatterNet().to(device=args.device)
    da_model = DeattenuateNet().to(device=args.device)
    
    bs_model.load_state_dict(torch.load(args.bs_weights))
    da_model.load_state_dict(torch.load(args.da_weights))
    
    skip_right = True
    total_bs_eval_time = 0.
    total_at_eval_time = 0.
    for j, (left, depth, fnames) in enumerate(dataloader):
        image_batch = left
        start = time()
        direct, backscatter = bs_model(image_batch, depth)
        total_bs_eval_time = time() - start
        direct_mean = direct.mean(dim=[2, 3], keepdim=True)
        direct_std = direct.std(dim=[2, 3], keepdim=True)
        direct_z = (direct - direct_mean) / direct_std
        clamped_z = torch.clamp(direct_z, -5, 5)
        direct_no_grad = torch.clamp(
            (clamped_z * direct_std) + torch.maximum(direct_mean, torch.Tensor([1. / 255]).to(device=args.device)), 0, 1).detach()
        start = time()
        f, J = da_model(direct_no_grad, depth)
        total_at_eval_time = time() - start
        total_time = total_bs_eval_time + total_at_eval_time
        print("Avg time per eval: %f ms (%f ms bs, %f ms at)" % (total_time, total_bs_eval_time, total_at_eval_time))
        direct_img = torch.clamp(direct_no_grad, 0., 1.).cpu()
        backscatter_img = torch.clamp(backscatter, 0., 1.).detach().cpu()
        f_img = f.detach().cpu()
        f_img = f_img / f_img.max()
        J_img = torch.clamp(J, 0., 1.).cpu()
        for side in range(1 if skip_right else 2):
            names = fnames[side]
            for n in range(1):
                i = n + target_batch_size * side
                if args.save_intermediates:
                    save_image(direct_img[i], "%s/%s-direct.png" % (save_dir, names[n].rstrip('.png')))
                    save_image(backscatter_img[i], "%s/%s-backscatter.png" % (save_dir, names[n].rstrip('.png')))
                    save_image(f_img[i], "%s/%s-f.png" % (save_dir, names[n].rstrip('.png')))
                save_image(J_img[i], "%s/%s-corrected.png" % (save_dir, names[n].rstrip('.png')))
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--images', type=str, required=True, help='Path to the images folder')
    parser.add_argument('--depth', type=str, required=True, help='Path to the depth folder')
    parser.add_argument('--output', type=str, required=True, help='Path to the output folder')
    parser.add_argument('--height', type=int, default=1242, help='Height of the image and depth files')
    parser.add_argument('--width', type=int, default=1952, help='Width of the image and depth')
    parser.add_argument('--depth_16u', action='store_true',
                        help='True if depth images are 16-bit unsigned (millimetres), false if floating point (metres)')
    parser.add_argument('--mask_max_depth', action='store_true',
                        help='If true will replace zeroes in depth files with max depth')
    parser.add_argument('--save_intermediates', action='store_true', default=False, help='Set to True to save intermediate files (backscatter, attenuation, and direct images)')
    parser.add_argument('--device', type=str, default='cuda:0' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--bs_weights', type=str, required=True, help='BS Model weights')
    parser.add_argument('--da_weights', type=str, required=True, help='DA Model weights')

    args = parser.parse_args()
    main(args)
