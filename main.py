import argparse
import os

import nsml
import torch
from nsml import DATASET_PATH
from torch import nn
from torch.utils.data import DataLoader
import pandas as pd

import nsml_utils 
from configuration.config import *
from data_loader import TagImageDataset
from utils import select_optimizer, select_model, evaluate, train
import random
import time
from self_training import df_teacher
from custom_loss import LabelSmoothingLoss
from models.trainable_embedding import Trainable_Embedding

def train_process(args, model, train_loader, test_loader, optimizer, unfroze_optimizer, criterion, device, threshold=3):
    best_acc = 0.0
    patience = 0.0
    best_f1 = 0.0
    logger.info(f"Trainable Parameters : {[ name for name,param in model.named_parameters() if param.requires_grad]}")

    for epoch in range(args.num_epoch):
        model.train()
        start = time.time()
        train_loss, train_acc = train(model=model, train_loader=train_loader, optimizer=optimizer,
                                      criterion=criterion, device=device, epoch=epoch, total_epochs=args.num_epoch + args.num_unfroze_epoch)
        end = time.time()
        model.eval()
        test_loss, test_acc, test_f1 = evaluate(model=model, test_loader=test_loader, device=device, criterion=criterion)

        report_dict = dict()
        report_dict["train__loss"] = train_loss
        report_dict["train__acc"] = train_acc
        report_dict["test__loss"] = test_loss
        report_dict["test__acc"] = test_acc
        report_dict["test__f1"] = test_f1
        report_dict["train__lr"] = optimizer.param_groups[0]['lr']
        nsml.report(False, step=epoch, **report_dict)

        logger.info(f"Time taken for epoch : {end-start}")
        if best_f1 < test_f1:
            checkpoint = 'best_'+str(epoch)
            logger.info(f'[{epoch}] Find the best model! Change the best model.')
            nsml.save(checkpoint)
            best_f1 = test_f1
            patience = 0        
        else:
            patience += 1

        if (epoch + 1) % 5 == 0:
            checkpoint = f'ckpt_{epoch + 1}'
            nsml.save(checkpoint)
        if patience > threshold:
            break

        if (epoch + 1) % args.annealing_period == 0:
            for g in optimizer.param_groups:
                g['lr'] = g['lr'] / args.learning_anneal
            logger.info('Learning rate annealed to : {lr:.6f} @epoch{epoch}'.format(
                epoch=epoch, lr=optimizer.param_groups[0]['lr']))
    ######################
    unfreeze(model)
    patience = 0 # init patience
    ######################
    for epoch in range(args.num_unfroze_epoch):
        model.train()
        start = time.time()
        train_loss, train_acc = train(model=model, train_loader=train_loader, optimizer=unfroze_optimizer,
                                      criterion=criterion, device=device, epoch=epoch+ args.num_epoch, total_epochs=args.num_epoch + args.num_unfroze_epoch)
        end = time.time()
        model.eval()
        test_loss, test_acc, test_f1 = evaluate(model=model, test_loader=test_loader, device=device, criterion=criterion)

        report_dict = dict()
        report_dict["train__loss"] = train_loss
        report_dict["train__acc"] = train_acc
        report_dict["test__loss"] = test_loss
        report_dict["test__acc"] = test_acc
        report_dict["test__f1"] = test_f1
        report_dict["train__lr"] = unfroze_optimizer.param_groups[0]['lr']
        nsml.report(False, step=epoch + args.num_epoch, **report_dict)

        logger.info(f"Time taken for epoch : {end-start}")
        if best_f1 < test_f1:
            checkpoint = 'best'
            logger.info(f'[{epoch}] Find the best model! Change the best model.')
            nsml.save(checkpoint)
            best_f1 = test_f1
            patience = 0        
        else:
            patience += 1

        if (epoch + 1) % 5 == 0:
            checkpoint = f'ckpt_{epoch + 1}'
            nsml.save(checkpoint)
        if patience > threshold:
            return 

        if (epoch + 1) % args.annealing_period == 0:
            for g in unfroze_optimizer.param_groups:
                g['lr'] = g['lr'] / args.learning_anneal
            logger.info('Learning rate annealed to : {lr:.6f} @epoch{epoch}'.format(
                epoch=epoch + args.num_epoch, lr=unfroze_optimizer.param_groups[0]['lr']))

def unfreeze(model):
    len_ = len(list(model.named_parameters()))
    for i, (name, params) in enumerate(model.named_parameters()):
        if 'bn' not in name and  i > len_ - 20:
            params.requires_grad = True
        
def load_weight(model, weight_file):
    """Load trained weight.
    You should put your weight file on the root directory with the name of `weight_file`.
    """
    if os.path.isfile(weight_file):
        model.load_state_dict(torch.load(weight_file).state_dict(), strict=True)
        print('load weight from {}.'.format(weight_file))
    else:
        print('weight file {} is not exist.'.format(weight_file))
        print('=> random initialized model will be used.')

def train_val_df(df, val_ratio = 0.2, n_class = 5, sed=None, oversample_ratio=[1, 1, 1, 1, 1]):
    columns = [col for col in df]
    trainData = [[] for i in range(n_class)]
    valData = [[] for i in range(n_class)]

    # class별로 정리
    for i in range(len(df['answer'])):
        item=[]
        for j in range(len(columns)):
            item.append(df[columns[j]][i])
        trainData[df['answer'][i]].append(item)

    # validation 빼놓기
    for i in range(n_class):
        len_td = len(trainData[i])
        val_num = int(len_td * val_ratio)
        if sed:
            random.seed(sed)
        num = [j for j in range(0, len_td)]
        val_num = sorted((random.sample(num, val_num)),reverse=True)
        for vn in val_num:
            valData[i].append(trainData[i].pop(vn))

    logger.info(f"origin class composition :  {[len(l) for l in trainData]} \t {[int(len(class_)/sum([len(l) for l in trainData])* 100) for class_ in trainData]}")
    logger.info(f"origin class composition : {[len(l) for l in valData]} \t {[int(len(class_)/sum([len(l) for l in valData])* 100) for class_ in valData]}")

    logger.info(f'orversampling ratio: {oversample_ratio} ')
    # oversampling 구현
    for i in range(n_class):
        if oversample_ratio[i] >= 1:
            trainData[i] = trainData[i] * int(oversample_ratio[i] // 1) 

            extra = int((oversample_ratio[i] % 1) * len(trainData[i]))
            trainData[i] += random.sample(trainData[i], extra) 
        else:
            trainData[i] = random.sample(trainData[i], int(len(trainData[i])* oversample_ratio[i]))
            # valData[i] = random.sample(valData[i], int(len(valData[i]) * oversample_ratio[i]))

    trainSet = []
    valSet = []
    for i in range(n_class):
        trainSet += trainData[i]
        valSet += valData[i]

    logger.info(f"Training Dataset size: {len(trainSet)} \tclass composition :  {[len(l) for l in trainData]} \t {[int(len(class_)/sum([len(l) for l in trainData])* 100) for class_ in trainData]}")
    logger.info(f"Validation Dataset size: {len(valSet)} \tclass composition : {[len(l) for l in valData]} \t {[int(len(class_)/sum([len(l) for l in valData])* 100) for class_ in valData]}")

    train_df = pd.DataFrame(trainSet, columns=columns)
    val_df = pd.DataFrame(valSet, columns=columns)

    return train_df, val_df

def main():
    # Argument Settings
    parser = argparse.ArgumentParser(description='Image Tagging Classification from Naver Shopping Reviews')
    parser.add_argument('--sess_name', default='', type=str, help='Session name that is loaded')
    parser.add_argument('--checkpoint', default='best', type=str, help='Checkpoint')
    parser.add_argument('--batch_size', default=256, type=int, help='batch size')
    parser.add_argument('--num_workers', default=16, type=int, help='The number of workers')
    parser.add_argument('--num_epoch', default=5, type=int, help='The number of epochs')
    parser.add_argument('--num_unfroze_epoch', default=5, type=int, help='The number of unfroze epochs')
    parser.add_argument('--model_name', default='resnext', type=str, help='[resnet50, resnext, dnet1244, dnet1222]')
    parser.add_argument('--optimizer', default='Adam', type=str)
    parser.add_argument('--unfroze_optimizer', default='Adam', type=str)
    parser.add_argument('--lr', default=1e-2, type=float)
    parser.add_argument('--unfroze_lr', default=1e-4, type=float)
    parser.add_argument('--weight_decay', default=1e-5, type=float)
    parser.add_argument('--learning_anneal', default=1.1, type=float)
    parser.add_argument('--annealing_period', default=10, type=int)
    parser.add_argument('--num_gpu', default=1, type=int)
    parser.add_argument('--pretrain', action='store_true', default=True)
    parser.add_argument('--mode', default='train', help='Mode')
    parser.add_argument('--pause', default=0, type=int)
    parser.add_argument('--iteration', default=0, type=str)
    parser.add_argument('--weight_file', default='model.pth', type=str)
    parser.add_argument('--self_training', default=False, type=str, help='t0019/rush2-2/126')
    parser.add_argument('--smooth', default=True, type=bool)
    parser.add_argument('--smooth_w', default=0.3, type=float)
    parser.add_argument('--smooth_att', default=1.5, type=float)
    parser.add_argument('--cat_embed', default=False, type=bool)
    parser.add_argument('--embed_dim', default=90, type=int)
    parser.add_argument('--onehot', default=0, type=int)
    args = parser.parse_args()

    # df setting by self-training
    if args.self_training and args.pause == 0:
        logger.info(f'self-training teacher sees : {args.self_training}')
        df = df_teacher(teacher_sess_name=args.self_training, undersample_ratio=[0.99, 0.99, 0.99, 0.99, 0.99], data_cross=False, onehot=args.onehot)
        logger.info('df by teacher')


    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    # Model
    logger.info('Build Model')
    model = select_model(args.model_name, pretrain=args.pretrain, n_class=5, onehot=args.onehot)
    total_param = sum([p.numel() for p in model.parameters()])
    #load_weight(model, args.weight_file)


    if args.model_name == 'efficientnet_b7':
        train_transform = efficientnet_transform
    elif args.model_name == 'efficientnet_b8':
        train_transform = efficientnetb8_transform
    else:
        train_transform = base_transform

    if args.cat_embed:
        logger.info("Trainable category embedding appended")
        model.use_fc_ = False
        model = Trainable_Embedding(model, embed_dim=args.embed_dim)

    logger.info(f'Model size: {total_param} tensors')
    model = model.to(device)
    nsml_utils.bind_model(model)

    if args.pause:
        nsml.paused(scope=locals())
    if args.num_epoch == 0:
        nsml.save('best')
        return

    # Set the dataset
    logger.info('Set the dataset')
    if args.self_training == False:
        df = pd.read_csv(f'{DATASET_PATH}/train/train_label')
        logger.info('normal df')
    # df = df.iloc[:5000]
    
    logger.info(f"Transformation on train dataset\n{train_transform}")
    train_df, val_df = train_val_df(df, oversample_ratio=[1, 1, 7, 1, 0.5], sed=42)
    trainset = TagImageDataset(data_frame=train_df, root_dir=f'{DATASET_PATH}/train/train_data',
                               transform=train_transform)
    testset = TagImageDataset(data_frame=val_df, root_dir=f'{DATASET_PATH}/train/train_data',
                              transform=test_transform)

    train_loader = DataLoader(dataset=trainset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers)
    test_loader = DataLoader(dataset=testset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers)

    if args.smooth:
        criterion = LabelSmoothingLoss(classes=5, smoothing=args.smooth_w, attention=args.smooth_att)
    else:
        criterion = nn.CrossEntropyLoss(reduction='mean')

    logger.info(f"Loss function : {criterion}")

    optimizer = select_optimizer(model.parameters(), args.optimizer, args.lr, args.weight_decay)
    unfroze_optimizer = select_optimizer(model.parameters(), args.unfroze_optimizer, args.unfroze_lr, args.weight_decay)


    criterion = criterion.to(device)

    if args.mode == 'train':
        logger.info('Start to train!')
        train_process(args=args, model=model, train_loader=train_loader, test_loader=test_loader,
                      optimizer=optimizer, unfroze_optimizer=unfroze_optimizer, criterion=criterion, device=device)

    elif args.mode == 'test':
        nsml.load(args.checkpoint, session=args.sess_name)
        logger.info('[NSML] Model loaded from {}'.format(args.checkpoint))

        model.eval()
        logger.info('Start to test!')
        test_loss, test_acc, test_f1 = evaluate(model=model, test_loader=test_loader, device=device,
                                                criterion=criterion)
        logger.info(f"loss = {test_loss}, accuracy = {test_acc}, F1-score = {test_f1}")
        nsml.save("best")

if __name__ == '__main__':
    main()
