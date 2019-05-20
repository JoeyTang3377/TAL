import sys
sys.path.append("..")
import torch
import torch.nn as nn
import torch.nn.functional as F
import pdb
import re
import numpy as np
import nets.resnet as resnet
import os
import math
import time
from utils.config import cfg
from utils.utils import proposal_nms
from layers.anchor_target_layer import anchor_target_layer
from layers.object_target_layer import object_target_layer
from layers.generate_anchor import generate_anchors
from nets.Relation import Relation, extract_position_embedding, extract_position_matrix

class Conv1d(nn.Module):
    
    def __init__(self, in_channels, out_channels, kernel_size, stride = 1, padding = 0, dilation = 1):
        super(Conv1d, self).__init__()
        if padding == 'SAME':
            pad = kernel_size + (kernel_size - 1)*(dilation - 1) - 1
            if pad % 2 == 0:
                self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, stride, padding = pad//2, dilation = dilation)
            else:
                self.conv = nn.Sequential(nn.ConstantPad1d((pad//2+1, pad//2), 0), nn.Conv1d(in_channels, out_channels, kernel_size, stride, dilation = dilation))
        elif padding == 'VALID':
            self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, stride, dilation = dilation)
        else:
            self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, stride, padding = padding, dilation = dilation)
    def __call__(self, inputs):
        return self.conv(inputs)

class MaxPool1d(nn.Module):
    
    def __init__(self, kernel_size, stride = 1, padding = 0, dilation = 1):
        super(MaxPool1d, self).__init__()
        if padding == 'SAME':
            pad = kernel_size + (kernel_size - 1)*(dilation - 1) - 1
            if pad % 2 == 0:
                self.pool = nn.MaxPool1d(kernel_size, stride, padding = pad//2, dilation = dilation)
            else:
                self.pool = nn.Sequential(nn.ConstantPad1d((pad//2+1, pad//2), 0), nn.MaxPool1d(kernel_size, stride, dilation = dilation))
        elif padding == 'VALID':
            self.pool = nn.MaxPool1d(kernel_size, stride, dilation = dilation)
        else:
            self.pool = nn.MaxPool1d(kernel_size, stride, padding = padding, dilation = dilation)
    def __call__(self, inputs):
        return self.pool(inputs)

class SegmentProposal(nn.Module):

    def __init__(self):
        super(SegmentProposal, self).__init__()
        self.relu = nn.ReLU(inplace = True)
        self.conv1 = Conv1d(256, 256, 3, 1, padding = 'SAME')
        self.conv_cls = Conv1d(256, int(2*len(cfg.Train.anchor_size)/cfg.Train.rpn_stride), 1, 1)
        self.conv_segment = Conv1d(256, int(2*len(cfg.Train.anchor_size)/cfg.Train.rpn_stride), 1, 1)
    
    def __call__(self, inputs):
        x = self.relu(self.conv1(inputs))
        cls_score = self.conv_cls(x)
        segment_pred = self.conv_segment(x)
        return cls_score, segment_pred

class RC3D(nn.Module):

    def __init__(self, num_classes, image_shape):
        super(RC3D, self).__init__()
        self.num_classes = num_classes
        self.anchor_size = cfg.Train.anchor_size
        self.num_anchors = len(self.anchor_size)
        (H, W) = image_shape
        assert H % 16 == 0, "H must be times of 16"
        assert W % 16 == 0, "W must be times of 16"
        self.layer = [64, '_M', 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M', 'fc', 'fc']
        self.relu = nn.ReLU(inplace = True)
        self.conv1 = nn.Conv1d(1024, 256, 1)
        self.conv2 = nn.Conv1d(1024, 256, 1)
        self.fc1 = nn.Linear(256*7, 256)
        self.cls = nn.Linear(256, self.num_classes)
        self.bbox_offset = nn.Linear(256, 2 * (self.num_classes - 1))
        self.segment_proposal = SegmentProposal()
        self.backbone = resnet.I3Res50(num_classes=self.num_classes)
        self.relation = Relation()

    def _cls_prob(self, inputs):
        inputs_reshaped = self._reshape(inputs)
        result = nn.Softmax(-1)(inputs_reshaped)

        return result
    
    def _reshape(self, inputs):
        inputs_reshape = inputs.transpose(2, 1)
        inputs_size = inputs_reshape.size()
        inputs_reshape = inputs_reshape.view(inputs_size[0], inputs_size[1], inputs_size[2] // 2, 2)

        return inputs_reshape

    def forward(self, inputs):
        self.im_info = inputs.size()[-1]
        feature = self.backbone(inputs) #[N, L/8, 1024]
        feature = feature.transpose(1, 2)
        x = self.relu(self.conv1(feature))
        cls_score, proposal_offset = self.segment_proposal(x)
        self.anchors = torch.tensor(generate_anchors(x.size()[-1], 8, cfg.Train.rpn_stride, self.anchor_size), dtype = torch.float32, device='cuda')
        cls_prob = self._cls_prob(cls_score).reshape(-1, 2)
        proposal_offset_reshaped = self._reshape(proposal_offset).reshape(-1, 2)
        proposal_idx, proposal_bbox = proposal_nms(self.anchors, cls_prob, proposal_offset_reshaped)
        proposal_offset = proposal_offset_reshaped[proposal_idx]
        #proposal_prob = cls_prob[proposal_idx]
        new_proposal = torch.empty_like(proposal_bbox, dtype = torch.int32)
        new_proposal = torch.empty_like(proposal_bbox, dtype = torch.int32)
        new_proposal[:, 0] = torch.min(torch.max(torch.floor((proposal_bbox[:, 0] - proposal_bbox[:, 1]) / 8), torch.zeros_like(proposal_bbox[:, 0])), torch.ones_like(proposal_bbox[:, 1]) * (feature.size()[-1] - 1)).long()
        new_proposal[:, 1] = torch.max(torch.min(torch.ceil((proposal_bbox[:, 0] + proposal_bbox[:, 1]) / 8), torch.ones_like(proposal_bbox[:, 1]) * (feature.size()[-1] - 1)), torch.zeros_like(proposal_bbox[:, 0])).long()
        self.proposal_bbox = proposal_bbox
        for i in range(new_proposal.size()[0]):
            #if new_proposal[i, 0] > new_proposal[i, 1]:
                #pdb.set_trace()
            if i == 0:
                spp_feature = nn.AdaptiveMaxPool1d((7))(feature[:, :, new_proposal[i, 0] : new_proposal[i, 1] + 1])
                #spp_feature = self.soipooling(feature[:, :, new_proposal[i, 0] : new_proposal[i, 1]])
            else:
                spp_feature = torch.cat((spp_feature, nn.AdaptiveMaxPool1d((7))(feature[:, :, new_proposal[i, 0] : new_proposal[i, 1] + 1])), 0)
                #spp_feature = torch.cat((spp_feature, self.soipooling(feature[:, :, new_proposal[i, 0] : new_proposal[i, 1]])), 0)
        x = self.relu(self.conv2(spp_feature))
        x = x.view(x.size()[0], -1)
        x = self.fc1(x)
        #这里的nongt_dim 可以选取别的值
        #TODO 选择映射后的且扩大感受野的ROI区域
        nongt_dim = x.shape[0]
        position_matrix = extract_position_matrix(new_proposal, nongt_dim)
        position_embedding = extract_position_embedding(position_matrix, cfg.Train.embedding_feat_dim)
        attention = self.relation.forward(x, position_embedding, nongt_dim)
        x = self.relu(x + attention)
        object_cls_score = self.cls(x)
        object_offset = self.bbox_offset(x)
        object_offset = object_offset.reshape(-1, self.num_classes - 1, 2)
        cls_score = self._reshape(cls_score).reshape(-1, 2)
        cls_score = cls_score[proposal_idx]
        self.anchors_new = self.anchors[proposal_idx]
        #pdb.set_trace()
        return cls_score, proposal_offset, object_cls_score, object_offset

    def get_loss(self, cls_score, proposal_offset, object_cls_score, object_offset, gt_boxes):#gt_boxes [N, 3] (idx, start, end) idx中类别也是从1开始
        #pdb.set_trace()
        rpn_label, rpn_bbox_offset = anchor_target_layer(gt_boxes[:, 1:], self.im_info, self.anchors_new)
        object_label, object_bbox_offset = object_target_layer(gt_boxes, self.im_info, self.proposal_bbox)
        rpn_label = rpn_label.view(-1, )
        object_label = object_label.view(-1, )
        #为分类问题增加权重
        #rpn_cls_loss
        e_index = torch.nonzero(rpn_label != -1).reshape(-1)
        e_index1 = torch.nonzero(rpn_label == 1).reshape(-1)
        e_index2 = torch.nonzero(object_label != -1).reshape(-1)
        e_index3 = torch.nonzero(object_label > 0).reshape(-1)
        cls_score = cls_score[e_index]
        rpn_label = rpn_label[e_index]
        positive_num = (rpn_label == 1).sum()
        negative_num = (rpn_label == 0).sum()
        cls_rpn_weight = torch.tensor([(positive_num + negative_num)/negative_num, (positive_num + negative_num)/positive_num]).float()
        creterion = nn.CrossEntropyLoss(weight = cls_rpn_weight.cuda())
        loss1 = creterion(cls_score, rpn_label.long())
        #rpn_bbox_loss
        proposal_offset = proposal_offset[e_index1]
        rpn_bbox_offset = rpn_bbox_offset[e_index1]
        loss2 = nn.SmoothL1Loss(reduction = 'mean')(proposal_offset, rpn_bbox_offset)
        #object_cls_loss
        cls_object_weight = torch.empty(self.num_classes).float()
        positive_num = (object_label > 0).sum()
        negative_num = (object_label == 0).sum()
        cls_object_weight[0] = (positive_num + negative_num)/negative_num
        cls_object_weight[1:] = (positive_num + negative_num)/positive_num
        creterion = nn.CrossEntropyLoss(weight = cls_object_weight.cuda())
        loss3 = creterion(object_cls_score[e_index2], object_label[e_index2].long())
        #object_bbox_loss
        object_offset = object_offset.reshape(-1, 2)
        e_index4 = e_index3 * (self.num_classes - 1) + object_label[e_index3].long() - 1
        loss4 = nn.SmoothL1Loss(reduction = 'mean')(object_offset[e_index4], object_bbox_offset[e_index3])
        if math.isnan(loss2.data) or math.isnan(loss4.data):
            print(loss1.data, loss2.data, loss3.data, loss4.data)
            pdb.set_trace()
        return loss1 + cfg.Train.regularization * loss2 + cfg.Train.cls_regularization * loss3 + cfg.Train.regularization * loss4, loss1, loss2, loss3, loss4

    def load(self, path, ltype=1):
        '''
        @params path 参数模型位置
        @ltype 0 读取预训练模型
               1 读取参数模型
        '''
        prefix = ''
        if ltype == 0:
            prefix = 'backbone.'
        try:
            pretrained_dict = torch.load(path)
            print("Begin to load model {} ...".format(path))
            model_dict = self.state_dict()
            pretrained_dict = {prefix + k:v for k, v in pretrained_dict.items() if prefix + k in model_dict.keys()}
            model_dict.update(pretrained_dict)
            self.load_state_dict(model_dict)
            print("Done!")
            del pretrained_dict
            del model_dict
        except Exception:
            print("Error! There is no model in ", os.path.join(os.path(), path))

    def save(self, path):
        print("Begin to load {} ...".format(path))
        f = open(path, 'wb')
        torch.save(self.state_dict(), f)
        f.close()
        print("Done!")