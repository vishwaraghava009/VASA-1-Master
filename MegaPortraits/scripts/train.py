# scripts/train.py

import argparse
import yaml
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import transforms
from models.appearance_encoder import AppearanceEncoder
from models.motion_encoder import MotionEncoder
from models.warping_generators import WarpingGenerator
from models.conv3d import Conv3D
from models.conv2d import Conv2D
from models.high_res_model import HighResModel
from models.student_model import StudentModel
from losses.perceptual_loss import PerceptualLoss
from losses.adversarial_loss import AdversarialLoss
from losses.cycle_consistency_loss import CycleConsistencyLoss
from losses.pairwise_loss import PairwiseLoss
from losses.cosine_similarity_loss import CosineSimilarityLoss
from utils.logger import setup_logger
from utils.checkpoint import save_checkpoint, load_checkpoint
from datasets.dataset import MegaPortraitDataset

class Trainer:
    def __init__(self, config, model_type):
        self.config = config
        self.model_type = model_type
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        if self.model_type == 'base':
            self.setup_base_model()
        elif self.model_type == 'highres':
            self.setup_high_res_model()
        elif self.model_type == 'student':
            self.setup_student_model()

        self.logger = setup_logger('train', self.config['log_path'])

    def setup_base_model(self):
        self.appearance_encoder = AppearanceEncoder().to(self.device)
        self.motion_encoder = MotionEncoder().to(self.device)
        self.warping_generator_s = WarpingGenerator().to(self.device)
        self.warping_generator_d = WarpingGenerator().to(self.device)
        self.conv3d = Conv3D().to(self.device)
        self.conv2d = Conv2D().to(self.device)
        self.discriminator = PatchGANDiscriminator().to(self.device)

        self.perceptual_loss = PerceptualLoss().to(self.device)
        self.adversarial_loss = AdversarialLoss().to(self.device)
        self.cycle_consistency_loss = CycleConsistencyLoss().to(self.device)
        self.pairwise_loss = PairwiseLoss().to(self.device)
        self.cosine_similarity_loss = CosineSimilarityLoss().to(self.device)

        self.optimizer_G = torch.optim.AdamW(
            list(self.appearance_encoder.parameters()) +
            list(self.motion_encoder.parameters()) +
            list(self.warping_generator_s.parameters()) +
            list(self.warping_generator_d.parameters()) +
            list(self.conv3d.parameters()) +
            list(self.conv2d.parameters()),
            lr=self.config['lr'],
            betas=(0.5, 0.999),
            eps=1e-8,
            weight_decay=1e-2
        )

        self.optimizer_D = torch.optim.AdamW(
            self.discriminator.parameters(),
            lr=self.config['lr'],
            betas=(0.5, 0.999),
            eps=1e-8,
            weight_decay=1e-2
        )

    def setup_high_res_model(self):
        self.model = HighResModel().to(self.device)
        self.discriminator = PatchGANDiscriminator().to(self.device)

        self.l1_loss = nn.L1Loss()
        self.perceptual_loss = PerceptualLoss().to(self.device)
        self.adversarial_loss = AdversarialLoss().to(self.device)
        self.cycle_consistency_loss = CycleConsistencyLoss().to(self.device)

        self.optimizer_G = torch.optim.AdamW(
            self.model.parameters(),
            lr=self.config['lr'],
            betas=(0.5, 0.999),
            eps=1e-8,
            weight_decay=1e-2
        )

        self.optimizer_D = torch.optim.AdamW(
            self.discriminator.parameters(),
            lr=self.config['lr'],
            betas=(0.5, 0.999),
            eps=1e-8,
            weight_decay=1e-2
        )

    def setup_student_model(self):
        self.model = StudentModel().to(self.device)
        self.discriminator = PatchGANDiscriminator().to(self.device)

        self.perceptual_loss = PerceptualLoss().to(self.device)
        self.adversarial_loss = AdversarialLoss().to(self.device)

        self.optimizer_G = torch.optim.AdamW(
            self.model.parameters(),
            lr=self.config['lr'],
            betas=(0.5, 0.999),
            eps=1e-8,
            weight_decay=1e-2
        )

        self.optimizer_D = torch.optim.AdamW(
            self.discriminator.parameters(),
            lr=self.config['lr'],
            betas=(0.5, 0.999),
            eps=1e-8,
            weight_decay=1e-2
        )

    def load_data(self):
        transform = transforms.Compose([
            transforms.ColorJitter(),
            transforms.RandomHorizontalFlip(),
            transforms.CenterCrop(224),
            transforms.ToTensor()
        ])
        train_dataset = MegaPortraitDataset(self.config['data_path'], transform)
        self.train_loader = DataLoader(train_dataset, batch_size=self.config['batch_size'], shuffle=True, num_workers=4)

    def train(self):
        start_epoch = 0
        if self.config['resume']:
            start_epoch = self.load_checkpoint()

        for epoch in range(start_epoch, self.config['epochs']):
            for i, data in enumerate(self.train_loader):
                source, target = data
                source = source.to(self.device)
                target = target.to(self.device)

                if self.model_type == 'base':
                    self.train_base_model(source, target)
                elif self.model_type == 'highres':
                    self.train_high_res_model(source, target)
                elif self.model_type == 'student':
                    self.train_student_model(source, target)

                if i % self.config['log_interval'] == 0:
                    self.logger.info(f"Epoch [{epoch}/{self.config['epochs']}], Step [{i}/{len(self.train_loader)}], "
                                     f"Loss: {self.total_loss.item():.4f}, D Loss: {self.d_loss.item():.4f}")

            if (epoch + 1) % self.config['checkpoint_interval'] == 0:
                self.save_checkpoint(epoch + 1)

    def train_base_model(self, source, target):
        self.optimizer_G.zero_grad()

        v_s = self.appearance_encoder(source)
        e_s = self.motion_encoder(source)
        R_s, t_s, z_s = e_s
        v_d = self.appearance_encoder(target)
        e_d = self.motion_encoder(target)
        R_d, t_d, z_d = e_d

        w_s = self.warping_generator_s(R_s, t_s, z_s, e_s)
        w_d = self.warping_generator_d(R_d, t_d, z_d, e_d)

        v_s_warped = self.conv3d(w_s)
        v_d_warped = self.conv3d(w_d)

        output = self.conv2d(v_s_warped)

        loss_perceptual = self.perceptual_loss(output, target)
        loss_adv = self.adversarial_loss(self.discriminator(target), self.discriminator(output))
        loss_cycle = self.cycle_consistency_loss(output, target)
        loss_pairwise = self.pairwise_loss(v_s, v_d)
        loss_cosine = self.cosine_similarity_loss(e_s, e_d)

        self.total_loss = loss_perceptual + loss_adv + loss_cycle + loss_pairwise + loss_cosine
        self.total_loss.backward()
        self.optimizer_G.step()

        self.optimizer_D.zero_grad()
        real_loss = self.adversarial_loss(self.discriminator(target), torch.ones_like(self.discriminator(target)))
        fake_loss = self.adversarial_loss(self.discriminator(output.detach()), torch.zeros_like(self.discriminator(output.detach())))
        self.d_loss = (real_loss + fake_loss) / 2
        self.d_loss.backward()
        self.optimizer_D.step()

    def train_high_res_model(self, source, target):
        self.optimizer_G.zero_grad()

        output = self.model(source)

        loss_l1 = self.l1_loss(output, target)
        loss_adv = self.adversarial_loss(self.discriminator(target), self.discriminator(output))
        loss_perceptual = self.perceptual_loss(output, target)
        loss_cycle = self.cycle_consistency_loss(output, target)

        self.total_loss = loss_l1 + loss_adv + loss_perceptual + loss_cycle
        self.total_loss.backward()
        self.optimizer_G.step()

        self.optimizer_D.zero_grad()
        real_loss = self.adversarial_loss(self.discriminator(target), torch.ones_like(self.discriminator(target)))
        fake_loss = self.adversarial_loss(self.discriminator(output.detach()), torch.zeros_like(self.discriminator(output.detach())))
        self.d_loss = (real_loss + fake_loss) / 2
        self.d_loss.backward()
        self.optimizer_D.step()

    def train_student_model(self, source, target):
        self.optimizer_G.zero_grad()

        output = self.model(source)

        loss_perceptual = self.perceptual_loss(output, target)
        loss_adv = self.adversarial_loss(self.discriminator(target), self.discriminator(output))

        self.total_loss = loss_perceptual + loss_adv
        self.total_loss```python
        self.total_loss.backward()
        self.optimizer_G.step()

        self.optimizer_D.zero_grad()
        real_loss = self.adversarial_loss(self.discriminator(target), torch.ones_like(self.discriminator(target)))
        fake_loss = self.adversarial_loss(self.discriminator(output.detach()), torch.zeros_like(self.discriminator(output.detach())))
        self.d_loss = (real_loss + fake_loss) / 2
        self.d_loss.backward()
        self.optimizer_D.step()

    def save_checkpoint(self, epoch):
        if self.model_type == 'base':
            save_checkpoint({
                'appearance_encoder': self.appearance_encoder.state_dict(),
                'motion_encoder': self.motion_encoder.state_dict(),
                'warping_generator_s': self.warping_generator_s.state_dict(),
                'warping_generator_d': self.warping_generator_d.state_dict(),
                'conv3d': self.conv3d.state_dict(),
                'conv2d': self.conv2d.state_dict(),
                'discriminator': self.discriminator.state_dict(),
                'optimizer_G': self.optimizer_G.state_dict(),
                'optimizer_D': self.optimizer_D.state_dict(),
                'epoch': epoch
            }, f"{self.config['checkpoint_path']}/base_model_epoch_{epoch}.pth")
        elif self.model_type == 'highres':
            save_checkpoint({
                'model': self.model.state_dict(),
                'discriminator': self.discriminator.state_dict(),
                'optimizer_G': self.optimizer_G.state_dict(),
                'optimizer_D': self.optimizer_D.state_dict(),
                'epoch': epoch
            }, f"{self.config['checkpoint_path']}/highres_model_epoch_{epoch}.pth")
        elif self.model_type == 'student':
            save_checkpoint({
                'model': self.model.state_dict(),
                'discriminator': self.discriminator.state_dict(),
                'optimizer_G': self.optimizer_G.state_dict(),
                'optimizer_D': self.optimizer_D.state_dict(),
                'epoch': epoch
            }, f"{self.config['checkpoint_path']}/student_model_epoch_{epoch}.pth")

    def load_checkpoint(self):
        if self.model_type == 'base':
            checkpoint = torch.load(f"{self.config['checkpoint_path']}/base_model_latest.pth", map_location=self.device)
            self.appearance_encoder.load_state_dict(checkpoint['appearance_encoder'])
            self.motion_encoder.load_state_dict(checkpoint['motion_encoder'])
            self.warping_generator_s.load_state_dict(checkpoint['warping_generator_s'])
            self.warping_generator_d.load_state_dict(checkpoint['warping_generator_d'])
            self.conv3d.load_state_dict(checkpoint['conv3d'])
            self.conv2d.load_state_dict(checkpoint['conv2d'])
            self.discriminator.load_state_dict(checkpoint['discriminator'])
            self.optimizer_G.load_state_dict(checkpoint['optimizer_G'])
            self.optimizer_D.load_state_dict(checkpoint['optimizer_D'])
            epoch = checkpoint['epoch']
        elif self.model_type == 'highres':
            checkpoint = torch.load(f"{self.config['checkpoint_path']}/highres_model_latest.pth", map_location=self.device)
            self.model.load_state_dict(checkpoint['model'])
            self.discriminator.load_state_dict(checkpoint['discriminator'])
            self.optimizer_G.load_state_dict(checkpoint['optimizer_G'])
            self.optimizer_D.load_state_dict(checkpoint['optimizer_D'])
            epoch = checkpoint['epoch']
        elif self.model_type == 'student':
            checkpoint = torch.load(f"{self.config['checkpoint_path']}/student_model_latest.pth", map_location=self.device)
            self.model.load_state_dict(checkpoint['model'])
            self.discriminator.load_state_dict(checkpoint['discriminator'])
            self.optimizer_G.load_state_dict(checkpoint['optimizer_G'])
            self.optimizer_D.load_state_dict(checkpoint['optimizer_D'])
            epoch = checkpoint['epoch']
        return epoch

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Training script for MegaPortraits')
    parser.add_argument('--config', type=str, required=True, help='Path to the config file')
    parser.add_argument('--model_type', type=str, choices=['base', 'highres', 'student'], required=True, help='Model type to train')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)

    trainer = Trainer(config, args.model_type)
    trainer.load_data()
    trainer.train()