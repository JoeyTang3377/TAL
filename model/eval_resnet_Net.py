#!/usr/bin/python3
import sys
sys.path.append("..")
import torch
import torch.nn as nn
import torch.nn.functional as F
import argparse
import os
import re
import json
import pdb
import nets.RC3D_resnet as RC3D_resnet
from utils.config import cfg
import numpy as np
import json
import utils.utils as utils

CLASSES = ("BackGround", "BaseballPitch", "BasketballDunk", "Billiards", "CleanAndJerk", "CliffDiving", "CricketBowling", "CricketShot", "Diving", "FrisbeeCatch",
            "GolfSwing", "HammerThrow", "HighJump", "JavelinThrow", "LongJump", "PoleVault", "Shotput", "SoccerPenalty", "TennisSwing", "ThrowDiscus", "VolleyballSpiking")
num_classes = len(CLASSES)
name_to_id = dict(list(zip(CLASSES, range(num_classes))))
id_to_name = dict(enumerate(CLASSES))

def arg_parse():
    parser = argparse.ArgumentParser(description = "ResNet")
    parser.add_argument("--image_path", dest = 'image_path', type = str, default = '/home/share2/zhangpengyi/data/ActionImage/')
    parser.add_argument("--annotation_path", dest = 'annotation_path', type = str, default = '/home/share2/zhangpengyi/data/ActionLabel/')
    parser.add_argument("--checkpoint_path", dest = 'checkpoint_path', type = str, default = '/home/share2/zhangpengyi/data/ActionCheckpoint/')
    parser.add_argument("--json_path", dest = 'json_path', type = str, default = '../Annotation/')
    parser.add_argument("--tiou", dest = 'tiou', type = float, default = 0.5)
    args = parser.parse_args()
    return args

def generate_det(args):
    ckpt_path = args.checkpoint_path
    try:
        names = os.listdir(ckpt_path)
        for name in names:
            out = re.findall("ResNet_.*", name)
            if out != []:
                ckpt_path = out[0]
                break
        ckpt_path = os.path.join(args.checkpoint_path, ckpt_path)
    except Exception:
        print("There is no checkpoint in ", args.checkpoint)
        exit
    model = RC3D_resnet.RC3D(num_classes, cfg.Test.Image_shape)
    model = model.cuda()
    model.zero_grad()
    model.load(ckpt_path)
    test_batch = utils.Batch_Generator(name_to_id, num_classes, args.image_path, args.annotation_path, mode = 'test')
    fp = []
    det = []
    for i in range(num_classes):
        f = open(os.path.join(args.json_path, "detection_{}.json".format(str(i + 1))), 'w')
        fp.append(f)
        det.append({})
        det[i]['object'] = []
    while True:
        with torch.no_grad():
            test_data, gt, name = next(test_batch)
            if gt.shape[0] == 0:
                break
            data = torch.tensor(test_data, device = 'cuda', dtype = torch.float32)
            _, _, object_cls_score, object_offset = model.forward(data)
            #bbox 是按照score降序排列的
            bbox = utils.nms(model.proposal_bbox, object_cls_score, object_offset, model.num_classes, model.im_info)
            for _cls, score, proposal in zip(bbox['cls'], bbox['score'], bbox['bbox']):
                if proposal[:, 0] == proposal[:, 1]:
                    continue
                temp_dict = {}
                temp_dict['file_name'] = name
                temp_dict['start'] = proposal[:, 0]
                temp_dict['end'] = proposal[:, 1]
                temp_dict['score'] = score
                det[int(_cls[0]) - 1]['object'].append(temp_dict)
            torch.cuda.empty_cache()
    for i in range(num_classes):
        json.dump(det[i], fp[i])
        fp[i].close()

def eval_ap(rec, prec):
    rec.insert(0, 0.)
    rec.append(1.)
    prec.insert(0, 0.)
    prec.append(0.)
    mrec = np.array(rec)
    mpre = np.array(prec)
    for i in range(len(mpre) - 1, 0, -1):
        mpre[i-1] = max(mpre[i-1], mpre[i])
    ris = np.where(mrec[1:] != mrec[:-1])[0]
    for ri in ris:
        ap += (mrec[ri + 1] - mrec[ri]) * mpre[ri + 1]
    return ap, mrec, mpre

def eval_mAP(args):
    AP = 0
    for cls_idx in range(num_classes):
        gt_json = open(os.path.join(args.json_path, "GT_{}.json".format(str(cls_idx + 1))))
        det_json = open(os.path.join(args.json_path, "detection_{}.json".format(str(cls_idx + 1))))
        gt = json.load(gt_json)
        det = json.load(det_json)
        fp = []
        tp = []
        score = []
        for idx in range(len(det[cls_idx]['object'])):
            score.append(det[cls_idx]['object']['score'])
        score = np.array(score)
        sort_idx = np.argsort(score).reshape(-1)
        temp_det = det[cls_idx]['object']
        temp_gt = gt[cls_idx]['object']
        for idx in range(len(temp_det)):
            ovm = -1
            result = -1
            for gt_idx in range(len(temp_gt)):
                if temp_det[gt_idx]['file_name'] != temp_gt[sort_idx[idx]]['file_name']:
                    continue
                intersection = max(min(temp_det[sort_idx[idx]]['end'], temp_gt[gt_idx]['end']) - max(temp_det[sort_idx[idx]]['start'], temp_gt[gt_idx]['start']) + 1, 0)
                overlap = intersection / (temp_det[sort_idx[idx]]['end'] - temp_det[sort_idx[idx]]['start'] + temp_gt[gt_idx]['end'] - temp_gt[gt_idx]['start'] + 2 - intersection)
                if overlap > ovm:
                    ovm = overlap
                    result = gt_idx
            if ovm >= args.tiou:
                if temp_gt[result]['use'] == False:
                    fp[idx] = 1
                else:
                    temp_gt[result]['use'] = True
                    tp[idx] = 1
            else:
                fp[idx] = 1
        total = 0
        for idx, val in enumerate(fp):
            fp[idx] += total
            total += val
        total = 0
        for idx, val in enumerate(tp):
            tp[idx] += total
            total += val
        rec = tp
        for idx, val in enumerate(tp):
            rec[idx] = float(tp[idx]) / temp_gt['num']
        prec = tp
        for idx, val in enumerate(tp):
            prec[idx] = float(tp[idx]) / (fp[idx] + tp[idx])

        ap, _, _ = eval_ap(rec, prec)
        AP += ap
    mAP = AP/num_classes
    return mAP

if __name__ == '__main__':
    args = arg_parse()
    print(args)
    utils.generate_gt(args.annotation_path)
    generate_det(args)
    eval_mAP(args)