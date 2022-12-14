import torch
from torch.backends import cudnn
cudnn.enabled = True
from torch.utils.data import DataLoader
from pipeline.dataset import DataSet
import sys
sys.path.append('utils')
import pyutils, torchutils, indexing
import importlib
from pipeline.dataloader import VOC12AffinityDataset, VOC12ImageDataset
from pipeline.resnet_irn import AffinityDisplacementLoss

def run(args):

    path_index = indexing.PathIndex(radius=10, default_size=(args.irn_crop_size // 4, args.irn_crop_size // 4))

    model = AffinityDisplacementLoss(path_index)

    file = "data/public/test.json"
    train_dataset = VOC12AffinityDataset(file,
                                label_dir=args.ir_label_out_dir,
                                voc12_root="public/test_image/",
                                indices_from=path_index.src_indices,
                                indices_to=path_index.dst_indices,
                                hor_flip=True,
                                crop_size=args.irn_crop_size,
                                crop_method="random",
                                rescale=(0.5, 1.5)
                                )
    train_data_loader = DataLoader(train_dataset, batch_size=8,
                                   shuffle=True, num_workers=4, pin_memory=True, drop_last=True)

    max_step = (len(train_dataset) // 32) * args.irn_num_epoches

    param_groups = model.trainable_parameters()
    optimizer = torchutils.PolyOptimizer([
        {'params': param_groups[0], 'lr': 1*0.1, 'weight_decay': 1e-4},
        {'params': param_groups[1], 'lr': 10*0.1, 'weight_decay': 1e-4}
    ], lr=0.1, weight_decay=1e-4, max_step=max_step)
    torch.cuda.empty_cache()
    model = torch.nn.DataParallel(model).cuda()
    for name, param in model.named_parameters():                 
        if name.startswith('resnet50'): 
            param.requires_grad = False
    model.train()

    avg_meter = pyutils.AverageMeter()

    timer = pyutils.Timer()

    for ep in range(args.irn_num_epoches):

        print('Epoch %d/%d' % (ep+1, args.irn_num_epoches))

        for iter, pack in enumerate(train_data_loader):

            img = pack['img'].cuda(non_blocking=True)
            bg_pos_label = pack['aff_bg_pos_label'].cuda(non_blocking=True)
            fg_pos_label = pack['aff_fg_pos_label'].cuda(non_blocking=True)
            neg_label = pack['aff_neg_label'].cuda(non_blocking=True)

            pos_aff_loss, neg_aff_loss, dp_fg_loss, dp_bg_loss = model(img, True)

            bg_pos_aff_loss = torch.sum(bg_pos_label * pos_aff_loss) / (torch.sum(bg_pos_label) + 1e-5)
            fg_pos_aff_loss = torch.sum(fg_pos_label * pos_aff_loss) / (torch.sum(fg_pos_label) + 1e-5)
            pos_aff_loss = bg_pos_aff_loss / 2 + fg_pos_aff_loss / 2
            neg_aff_loss = torch.sum(neg_label * neg_aff_loss) / (torch.sum(neg_label) + 1e-5)

            dp_fg_loss = torch.sum(dp_fg_loss * torch.unsqueeze(fg_pos_label, 1)) / (2 * torch.sum(fg_pos_label) + 1e-5)
            dp_bg_loss = torch.sum(dp_bg_loss * torch.unsqueeze(bg_pos_label, 1)) / (2 * torch.sum(bg_pos_label) + 1e-5)

            avg_meter.add({'loss1': pos_aff_loss.item(), 'loss2': neg_aff_loss.item(),
                           'loss3': dp_fg_loss.item(), 'loss4': dp_bg_loss.item()})

            total_loss = (pos_aff_loss + neg_aff_loss) / 2 + (dp_fg_loss + dp_bg_loss) / 2

            optimizer.zero_grad()
            total_loss.backward()
            optimizer.step()

            if (optimizer.global_step - 1) % 50 == 0:
                timer.update_progress(optimizer.global_step / max_step)

                print('step:%5d/%5d' % (optimizer.global_step - 1, max_step),
                      'loss:%.4f %.4f %.4f %.4f' % (
                      avg_meter.pop('loss1'), avg_meter.pop('loss2'), avg_meter.pop('loss3'), avg_meter.pop('loss4')),
                      'imps:%.1f' % ((iter + 1) * args.irn_batch_size / timer.get_stage_elapsed()),
                      'lr: %.4f' % (optimizer.param_groups[0]['lr']),
                      'etc:%s' % (timer.str_estimated_complete()), flush=True)
        else:
            timer.reset_stage()
    file = "data/public/test.json"
    infer_dataset = VOC12ImageDataset(file,
                                        voc12_root=args.voc12_root,
                                        crop_size=args.irn_crop_size,
                                        crop_method="top_left")
    infer_data_loader = DataLoader(infer_dataset, batch_size=args.irn_batch_size,
                                   shuffle=False, num_workers=args.num_workers, pin_memory=True, drop_last=True)

    model.eval()
    print('Analyzing displacements mean ... ', end='')

    dp_mean_list = []

    with torch.no_grad():
        for iter, pack in enumerate(infer_data_loader):
            img = pack['img'].cuda(non_blocking=True)

            aff, dp = model(img, False)

            dp_mean_list.append(torch.mean(dp, dim=(0, 2, 3)).cpu())

        model.module.mean_shift.running_mean = torch.mean(torch.stack(dp_mean_list), dim=0)
    print('done.')

    torch.save(model.module.state_dict(), args.irn_weights_name)
    torch.cuda.empty_cache()
