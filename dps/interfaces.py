import string

import numpy as np
import torch as th

from ttools.training import ModelInterface

from . import utils, templates


class VectorizerInterface(ModelInterface):
    def __init__(self, model, args, cuda=True):
        self.model = model
        self.cuda = cuda
        self.args = args
        self._step = 0

        self.curve_templates = th.Tensor(templates.simple_templates if args.simple_templates
                else templates.letter_templates)

        if self.cuda:
            self.model.cuda()
            self.curve_templates = self.curve_templates.cuda()

        self.optimizer = th.optim.Adam(self.model.parameters(), lr=self.args.lr)

    def forward(self, batch):
        im = batch['im']
        n_loops = batch['n_loops']
        letter_idx = batch['letter_idx']
        if self.cuda:
            im = im.cuda()
            n_loops = n_loops.cuda()
            letter_idx = letter_idx.cuda()

        z = im.new_zeros(im.size(0), len(string.ascii_uppercase)).scatter_(1, letter_idx[:,None], 1)
        out = self.model(im, z)
        curves = out['curves']
        strokes = out['strokes'] * self.args.max_stroke

        distance_fields = utils.compute_distance_fields(curves, n_loops, templates.topology, self.args.canvas_size)
        alignment_fields = utils.compute_alignment_fields(distance_fields.min(1)[0])
        distance_fields = distance_fields[...,1:-1,1:-1]
        distance_fields = th.max(distance_fields-strokes[...,None,None], th.zeros_like(distance_fields)).min(1)[0]
        occupancy_fields = utils.compute_occupancy_fields(distance_fields)

        return {
            'curves': curves,
            'distance_fields': distance_fields,
            'alignment_fields': alignment_fields,
            'occupancy_fields': occupancy_fields
        }

    def _compute_lossses(self, batch, fwd_data):
        ret = {}

        target_distance_fields = batch['distance_fields']
        target_alignment_fields = batch['alignment_fields']
        target_occupancy_fields = batch['occupancy_fields']
        target_points = batch['points']
        letter_idx = batch['letter_idx']
        n_loops = batch['n_loops']
        if self.cuda:
            target_distance_fields = target_distance_fields.cuda()
            target_alignment_fields = target_alignment_fields.cuda()
            target_occupancy_fields = target_occupancy_fields.cuda()
            letter_idx = letter_idx.cuda()
            n_loops = n_loops.cuda()
            if target_points.numel() > 0:
                target_points = target_points.cuda()

        distance_fields = fwd_data['distance_fields']
        alignment_fields = fwd_data['alignment_fields']
        occupancy_fields = fwd_data['occupancy_fields']
        curves = fwd_data['curves']

        globalloss = th.mean((distance_fields-target_distance_fields)**2)
        surfaceloss = th.mean(target_occupancy_fields*distance_fields + target_distance_fields*occupancy_fields)
        alignmentloss = th.mean(1 - th.sum(target_alignment_fields*alignment_fields, dim=-1)**2)
        chamferloss = None
        if self.args.chamfer is not None:
            chamferloss = utils.compute_chamfer_distance(
                    utils.sample_points_from_curves(curves, n_loops, templates.topology, self.args.n_samples_per_curve),
                    target_points)
            ret['chamferloss'] = chamferloss
        ret['globalloss'] = globalloss
        ret['surfaceloss'] = surfaceloss
        ret['alignmentloss'] = alignmentloss

        templateloss = 0
        b = curves.size(0)
        curve_templates = self.curve_templates.index_select(0, letter_idx)
        template_loops = th.split(curve_templates.view(b, -1, 2), [2*n for n in templates.topology], dim=1)
        loops = th.split(curves.view(b, -1, 2), [2*n for n in templates.topology], dim=1)
        for i, (template_loop, loop) in enumerate(zip(template_loops, loops)):
            idxs = (n_loops>i).nonzero().squeeze()
            if idxs.numel() == 0:
               break
            templateloss += th.mean((loop.index_select(0, idxs) - template_loop.index_select(0, idxs)) ** 2)
        ret['templateloss'] = templateloss

        w_template = self.args.w_template*np.exp(-max(self._step-1500, 0)/500)
        loss = chamferloss if self.args.chamfer == "optimize" else self.args.w_surface*surfaceloss + \
                self.args.w_global*globalloss + self.args.w_alignment*alignmentloss + w_template*templateloss
        ret['loss'] = loss

        return ret

    def backward(self, batch, fwd_data):
        self.optimizer.zero_grad()

        losses_dict = self._compute_lossses(batch, fwd_data)
        loss = losses_dict['loss']

        loss.backward()
        self.optimizer.step()
        self._step += 1

        return { k: v.item() for k, v in losses_dict.items() }

    def init_validation(self):
        return { 'loss': 0, 'count': 0 }

    def update_validation(self, batch, fwd_data, running_data):
        n = batch['im'].shape[0]
        losses_dict = self._compute_lossses(batch, fwd_data)
        loss = losses_dict['loss']
        return {
            'loss': running_data['loss'] + loss.item()*n,
            'count': running_data['count'] + n
        }

    def finalize_validation(self, running_data):
        return { 'loss': running_data['loss'] / running_data['count'] }
