from __future__ import print_function

import argparse
import os
import shutil
import time
import random

import numpy as np

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data as data
import torchvision.transforms as transforms
import torch.nn.functional as F
from torch.autograd import Variable
from collections import OrderedDict

import models.tcdcnn as models
import dataset.aflw as dataset
from utils import Bar, Logger, AverageMeter, accuracy, mkdir_p, savefig
from tensorboardX import SummaryWriter
import pdb


parser = argparse.ArgumentParser(description='PyTorch Face Keypoints Regression')
# Optimization options
parser.add_argument('--epochs', default=150, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--batch-size', default=128, type=int, metavar='N',
                    help='train batchsize')
parser.add_argument('--lr', '--learning-rate', default=3e-2, type=float,
                    metavar='LR', help='initial learning rate')
# Checkpoints
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
# Miscs
parser.add_argument('--manualSeed', type=int, default=0, help='manual seed')
#Device options
parser.add_argument('--gpu', default='0', type=str,
                    help='id(s) for CUDA_VISIBLE_DEVICES')
#Method options
parser.add_argument('--n-labeled', type=float, default=-1,
                        help='Number of labeled data')
parser.add_argument('--val-iteration', type=int, default=150,
                        help='Number of labeled data')
parser.add_argument('--out', default='result',
                        help='Directory to output the result')
parser.add_argument('--alpha', default=0.75, type=float)
parser.add_argument('--lambda-u', default=75, type=float)
parser.add_argument('--T', default=0.5, type=float)
parser.add_argument('--ema-decay', default=0.999, type=float)

args = parser.parse_args()
state = {k: v for k, v in args._get_kwargs()}

# Use CUDA
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
use_cuda = torch.cuda.is_available()

# Random seed
if args.manualSeed is None:
    args.manualSeed = random.randint(1, 10000)
np.random.seed(args.manualSeed)
torch.manual_seed(args.manualSeed)

best_error = 100  # best test accuracy
mean = 0
std = 0
global_step = 0

def main():
    global best_error
    global mean
    global std

    if not os.path.isdir(args.out):
    	mkdir_p(args.out)
    # Data
    print(f'==> Preparing aflw')
    transform_train = transforms.Compose([
        transforms.Resize((60,60)),
        ])
    transform_val = transforms.Compose([
        transforms.Resize((60,60)),
        ])
    num_workers = 24
    train_labeled_set, train_unlabeled_set, stat_labeled_set, train_val_set, val_set, test_set, mean, std = dataset.get_aflw('./data/aflw_release-2/', args.n_labeled, transform_train, transform_val)
    labeled_trainloader = data.DataLoader(train_labeled_set, batch_size=args.batch_size, shuffle=True, num_workers=num_workers, drop_last=True)
    unlabeled_trainloader = data.DataLoader(train_unlabeled_set, batch_size=args.batch_size, shuffle=True, num_workers=num_workers, drop_last=True)
    # Here, train_val_set is NOT the validation set. It is the labeled training set with a different
    # shuffle order. All validation data are used for hyper-parameters selection and early stopping
    # So we do not use any additional data for training.
    train_val_loader = data.DataLoader(train_val_set, batch_size=args.batch_size, shuffle=True, num_workers=num_workers, drop_last=True)
    val_loader = data.DataLoader(val_set, batch_size=args.batch_size, shuffle=False, num_workers=num_workers)
    test_loader = data.DataLoader(test_set, batch_size=args.batch_size, shuffle=False, num_workers=num_workers)


    # Model
    print("==> creating TCDCN")

    def create_model(ema=False):
        model = models.TCDCNN()
        model = model.cuda()

        if ema:
            for param in model.parameters():
                param.detach_()

        return model

    model = create_model()
    tmp_model = create_model()
    ema_model = create_model(ema=True)
    target_model = create_model(ema=True)

    cudnn.benchmark = True
    print('    Total params: %.2fM' % (sum(p.numel() for p in model.parameters())/1000000.0))

    train_criterion = nn.MSELoss()
    criterion = nn.MSELoss()
    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)


    ema_optimizer= WeightEMA(model, ema_model, alpha=args.ema_decay)
    target_optimizer = UpdateEma(model, target_model, alpha=args.ema_decay)
    start_epoch = 0
    rampup_length = float(args.epochs) / float(args.batch_size)
    # Resume
    title = 'AFLW'
    if args.resume:
        # Load.
        print('==> Resuming from checkpoint..')
        assert os.path.isfile(args.resume), 'Error: no checkpoint directory found!'
        args.out = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        best_error = checkpoint['best_error']
        start_epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['state_dict'])
        ema_model.load_state_dict(checkpoint['ema_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        logger = Logger(os.path.join(args.resume, 'log.txt'), title=title, resume=True)
    else:
        logger = Logger(os.path.join(args.out, 'log.txt'), title=title)
        logger.set_names(['Train Loss', 'Train Loss X', 'Train Loss U', 'Train ME', 'Train FR' , 'Valid Loss', 'Valid ME.', 'Valid FR', 'Test Loss', 'Test ME.', 'Test FR'])

    writer = SummaryWriter(args.out)
    step = 0
    test_accs = []
    # Train and val
    for epoch in range(start_epoch, args.epochs):
        if (epoch + 1) % 5 == 0 and (epoch + 1) <= args.epochs:
            optimizer.param_groups[0]['lr'] *= 0.1

        print('\nEpoch: [%d | %d] LR: %f' % (epoch + 1, args.epochs, state['lr']))

        train_loss, train_loss_x, train_loss_u = train(labeled_trainloader, unlabeled_trainloader, train_val_loader, model, target_model, tmp_model, optimizer, ema_optimizer, target_optimizer, train_criterion, epoch, use_cuda, rampup_length)
        _, train_me, train_fr = validate(labeled_trainloader, model, criterion, epoch, use_cuda, mode='Train Stats')
        val_loss, val_me, val_fr = validate(val_loader, model, criterion, epoch, use_cuda, mode='Valid Stats')
        test_loss, test_me, test_fr = validate(test_loader, model, criterion, epoch, use_cuda, mode='Test Stats ')

        step = args.batch_size * args.val_iteration * (epoch + 1)

        writer.add_scalar('losses/train_loss', train_loss, step)
        writer.add_scalar('losses/valid_loss', val_loss, step)
        writer.add_scalar('losses/test_loss', test_loss, step)

        writer.add_scalar('accuracy/train_me', train_me, step)
        writer.add_scalar('accuracy/val_me', val_me, step)
        writer.add_scalar('accuracy/test_me', test_me, step)

        writer.add_scalar('accuracy/train_fr', train_fr, step)
        writer.add_scalar('accuracy/val_fr', val_fr, step)
        writer.add_scalar('accuracy/test_fr', test_fr, step)
        
        # scheduler.step()

        # append logger file
        logger.append([train_loss, train_loss_x, train_loss_u, train_me, train_fr, val_loss, val_me, val_fr, test_loss, test_me, test_fr])

        # save model
        is_best = val_me < best_error
        best_error = min(val_me, best_error)
        if is_best:
            best_test = test_me
        save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'ema_state_dict': ema_model.state_dict(),
                'me': val_me,
                'best_error': best_error,
                'optimizer' : optimizer.state_dict(),
            }, is_best)
        test_accs.append(test_me)
    logger.close()
    writer.close()

    print('Best acc')
    print(best_test)


def train(labeled_trainloader, unlabeled_trainloader, train_val_loader, model, target_model, tmp_model, optimizer, ema_optimizer, target_optimizer, criterion, epoch, use_cuda, rampup_length):

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_u = AverageMeter()
    ws = AverageMeter()
    end = time.time()
    global global_step

    bar = Bar('Training', max=args.val_iteration)
    labeled_train_iter = iter(labeled_trainloader)
    unlabeled_train_iter = iter(unlabeled_trainloader)

    
    model.train()
    tmp_model.train()
    for batch_idx in range(args.val_iteration):
        try:
            inputs_x, targets_x = labeled_train_iter.next()
        except:
            labeled_train_iter = iter(labeled_trainloader)
            inputs_x, targets_x = labeled_train_iter.next()

        try:
            inputs_u, x1, y1, inputs_u2, x2, y2 = unlabeled_train_iter.next()
        except:
            unlabeled_train_iter = iter(unlabeled_trainloader)
            inputs_u, x1, y1, inputs_u2, x2, y2 = unlabeled_train_iter.next()

        try:
            inputs_val, targets_val = train_val_iter.next()
        except:
            train_val_iter = iter(train_val_loader)
            inputs_val, targets_val = train_val_iter.next()

        # measure data loading time
        data_time.update(time.time() - end)

        batch_size = inputs_x.size(0)


        if use_cuda:
            inputs_x, targets_x = inputs_x.cuda(), targets_x.cuda(non_blocking=True)
            inputs_u, inputs_u2 = inputs_u.cuda(), inputs_u2.cuda()
            x1, y1, x2, y2 = x1.cuda(), y1.cuda(), x2.cuda(), y2.cuda()
            inputs_val, targets_val = inputs_val.cuda(), targets_val.cuda(non_blocking=True)
        targets_val = (targets_val - mean) / std


        with torch.no_grad():
            # guessing labels
            outputs_u2 = target_model(inputs_u2)
            p = outputs_u2 * std + mean
            pt = p.view(p.size(0), 5, 2)
            # pdb.set_trace()
            pt[:,:,0] = pt[:,:,0] - x2.unsqueeze(1).float() + x1.unsqueeze(1).float()
            pt[:,:,1] = pt[:,:,1] - y2.unsqueeze(1).float() + y1.unsqueeze(1).float()
            targets_u = pt.view(pt.size(0), -1)
            targets_u = (targets_u - mean) / std

        logits_x = model(inputs_x) 
        logits_u = model(inputs_u)
        targets_x = (targets_x - mean) / std

        Lx = criterion(logits_x, targets_x)
        Lu = ((logits_u - targets_u) ** 2).mean()
        w = linear_rampup(global_step, args.epochs * args.val_iteration * 0.4) * 1
        loss = Lx + w * Lu


        # record loss
        losses.update(loss.item(), inputs_x.size(0))
        losses_x.update(Lx.item(), inputs_x.size(0))
        losses_u.update(Lu.item(), inputs_x.size(0))
        ws.update(w, inputs_x.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        optimizer.zero_grad()

        for param, tmp_param in zip(model.parameters(), tmp_model.parameters()):
            tmp_param.data.copy_(param.data)
        p = tmp_model(inputs_u2)
        pt = (p * std + mean).view(p.size(0),5,2)
        pt[:,:,0] = pt[:,:,0] - x2.unsqueeze(1).float() + x1.unsqueeze(1).float()
        pt[:,:,1] = pt[:,:,1] - y2.unsqueeze(1).float() + y1.unsqueeze(1).float()
        targets_u = pt.view(pt.size(0), -1)
        targets_u = (targets_u - mean) / std

        logits_u = model(inputs_u)

        Lu_tmp = ((logits_u - targets_u) ** 2).mean()

        grads = torch.autograd.grad(Lu_tmp, model.parameters(), create_graph=True)
        state_params = {key: val.clone() for key, val in model.state_dict().items()}
        adapted_params = OrderedDict()
        tmp_lr = 0.15
        for (key, val), grad in zip(model.named_parameters(), grads):

            if grad is not None:
                adapted_params[key] = val.detach() - tmp_lr * grad
                state_params[key] = adapted_params[key]
            else:
                adapted_params[key] = val.detach()
                state_params[key] = adapted_params[key]
        train_val_outs = model(inputs_val, state_params)
        train_val_loss = ((train_val_outs - targets_val) ** 2).mean()
        meta_grads = torch.autograd.grad(train_val_loss, tmp_model.parameters())
        for (key, val), grad in zip(model.named_parameters(), meta_grads):
            val.grad = 1 * grad.detach()
        optimizer.step()
        optimizer.zero_grad()


        target_optimizer.step(global_step)
        global_step += 1

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.3f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | Loss_x: {loss_x:.4f} | Loss_u: {loss_u:.4f} | W: {w:.4f}'.format(
                    batch=batch_idx + 1,
                    size=args.val_iteration,
                    data=data_time.avg,
                    bt=batch_time.avg,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    loss_x=losses_x.avg,
                    loss_u=losses_u.avg,
                    w=ws.avg,
                    )
        bar.next()
    bar.finish()

    ema_optimizer.step(bn=True)

    return (losses.avg, losses_x.avg, losses_u.avg)

def validate(valloader, model, criterion, epoch, use_cuda, mode):


    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    ME = AverageMeter()
    FR = AverageMeter()
    global mean
    global std

    # switch to evaluate mode
    model.eval()

    end = time.time()
    bar = Bar(f'{mode}', max=len(valloader))
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(valloader):
            # measure data loading time
            data_time.update(time.time() - end)

            if use_cuda:
                inputs, targets = inputs.cuda(), targets.cuda(non_blocking=True)

            outputs = model(inputs)
            outputs = outputs * std + mean

            loss = criterion(outputs, targets)
            mean_error, failure_rate = evaluate(outputs, targets)

            # measure accuracy and record loss
            # prec1, prec5 = accuracy(outputs, targets, topk=(1, 5))
            losses.update(loss.item(), inputs.size(0))
            ME.update(mean_error.item(), inputs.size(0))
            FR.update(failure_rate.item(), inputs.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            # plot progress
            bar.suffix  = '({batch}/{size}) Data: {data:.3f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | mean error: {ME: .4f} | failure rate: {FR: .4f}'.format(
                        batch=batch_idx + 1,
                        size=len(valloader),
                        data=data_time.avg,
                        bt=batch_time.avg,
                        total=bar.elapsed_td,
                        eta=bar.eta_td,
                        loss=losses.avg,
                        ME=ME.avg,
                        FR=FR.avg,
                        )
            bar.next()
        bar.finish()
    return (losses.avg, ME.avg, FR.avg,)

def save_checkpoint(state, is_best, checkpoint=args.out, filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint, 'model_best.pth.tar'))

def linear_rampup(current, rampup_length=1024):
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current / rampup_length, 0.0, 1.0)
        return float(current)

class SemiLoss(object):
    def __call__(self, outputs_x, targets_x, outputs_u, targets_u, epoch, rampup_length):
        probs_u = torch.softmax(outputs_u, dim=1)

        Lx = -torch.mean(torch.sum(F.log_softmax(outputs_x, dim=1) * targets_x, dim=1))
        Lu = torch.mean((probs_u - targets_u)**2)

        return Lx, Lu, args.lambda_u * linear_rampup(epoch, rampup_length)

class WeightEMA(object):
    def __init__(self, model, ema_model, alpha=0.999):
        self.model = model
        self.ema_model = ema_model
        self.alpha = alpha
        self.tmp_model = models.TCDCNN().cuda()
        self.wd = 0.02 * args.lr

        for param, ema_param in zip(self.model.parameters(), self.ema_model.parameters()):
            ema_param.data.copy_(param.data)

    def step(self, bn=False):
        if bn:
            # copy batchnorm stats to ema model
            for ema_param, tmp_param in zip(self.ema_model.parameters(), self.tmp_model.parameters()):
                tmp_param.data.copy_(ema_param.data.detach())

            self.ema_model.load_state_dict(self.model.state_dict())

            for ema_param, tmp_param in zip(self.ema_model.parameters(), self.tmp_model.parameters()):
                ema_param.data.copy_(tmp_param.data.detach())
        else:
            one_minus_alpha = 1.0 - self.alpha
            for param, ema_param in zip(self.model.parameters(), self.ema_model.parameters()):
                ema_param.data.mul_(self.alpha)
                ema_param.data.add_(param.data.detach() * one_minus_alpha)
                # customized weight decay
                param.data.mul_(1 - self.wd)
class UpdateEma(object):
    def __init__(self, model, target_model, alpha=0.999):
        self.model = model
        self.target_model = target_model
        self.alpha = alpha
        for param, target_param in zip(self.model.parameters(), self.target_model.parameters()):
            target_param.data.copy_(param.data)
    def step(self, global_step):
        alpha = min(1 - 1 / (global_step + 1), self.alpha)
        for target_param, param in zip(self.target_model.parameters(), self.model.parameters()):
            target_param.data.mul_(alpha).add_(1-alpha, param.data)

def interleave_offsets(batch, nu):
    groups = [batch // (nu + 1)] * (nu + 1)
    for x in range(batch - sum(groups)):
        groups[-x - 1] += 1
    offsets = [0]
    for g in groups:
        offsets.append(offsets[-1] + g)
    assert offsets[-1] == batch
    return offsets


def interleave(xy, batch):
    nu = len(xy) - 1
    offsets = interleave_offsets(batch, nu)
    xy = [[v[offsets[p]:offsets[p + 1]] for p in range(nu + 1)] for v in xy]
    for i in range(1, nu + 1):
        xy[0][i], xy[i][i] = xy[i][i], xy[0][i]
    return [torch.cat(v, dim=0) for v in xy]

def evaluate(preds, targets):
    # pdb.set_trace()
    # here, preds are a vector predicted by the network and targets is the corresponding gt vector
    preds = preds.view(-1,5,2)
    targets = targets.view(-1,5,2)
	# occular_distance
    eyes = targets[:,:2,:]
    occular_distance = ((eyes[:,0,:] - eyes[:,1,:]) ** 2).sum(dim=-1).sqrt()
    distances = ((preds - targets) ** 2).sum(dim=-1).sqrt()
    mean_error = (distances / occular_distance.unsqueeze(1)).mean(dim=-1)
    failures = torch.zeros(preds.size(0)).float()
    failures[mean_error > 0.1] = 1.0
    return mean_error.mean(), failures.mean()



if __name__ == '__main__':
    main()


















