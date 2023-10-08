import torch
import torch.nn as nn
import torch.nn.functional as F
import random


class LHMLP_Se(nn.Module):
    def __init__(self, dataset, data_size, hidden, nclass,
                 num_feats, feat_keys, num_label_feats, label_feat_keys, tgt_key,
                 dropout, input_drop, label_drop, device, num_final, residual=False,
                 label_residual=True, num_sampled=0, num_label=0):
        
        super(LHMLP_Se, self).__init__()

        self.num_sampled = num_sampled
        # self.label_sampled = num_label if num_label_feats else 0
        self.all_meta_path = list(feat_keys) + list(label_feat_keys)
        self.dataset = dataset
        self.residual = residual
        self.tgt_key = tgt_key
        self.label_residual = label_residual

        self.num_feats = num_feats
        self.num_label_feats = num_label_feats
        self.num_paths = num_feats + num_label_feats
        self.num_final = num_final
        self.num_res = self.num_paths - self.num_final
        print("number of paths", num_feats, num_label_feats)

        self.embeding = nn.ParameterDict({})
        for k, v in data_size.items():
            self.embeding[str(k)] = nn.Parameter(
                torch.Tensor(v, hidden).uniform_(-0.5, 0.5))

        if len(label_feat_keys):
            self.labels_embeding = nn.ParameterDict({})
            for k in label_feat_keys:
                self.labels_embeding[k] = nn.Parameter(
                    torch.Tensor(nclass, hidden).uniform_(-0.5, 0.5))

        self.lr_output = nn.Sequential(
            nn.Linear(hidden, nclass, bias=False),
            nn.BatchNorm1d(nclass)
        )

        self.prelu = nn.PReLU()
        self.dropout = nn.Dropout(dropout)
        self.input_drop = nn.Dropout(input_drop)

        self.alpha = torch.ones(self.num_paths).to(device)
        self.alpha.requires_grad_(True)

        if self.residual:
            self.res_fc = nn.Linear(hidden, hidden)

        if self.label_residual:
            self.label_res_fc = nn.Linear(nclass, nclass)
            self.label_drop = nn.Dropout(label_drop)

        self.init_params()

    def init_params(self):

        gain = nn.init.calculate_gain("relu")

        for layer in self.lr_output:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight, gain=gain)
                if layer.bias is not None:
                    nn.init.zeros_(layer.bias)


    def alphas(self):
        alphas= [self.alpha]
        return alphas


    def epoch_sample(self, eps, key):
        indices = torch.argsort(self.alpha, dim=-1, descending=True)[:int(self.num_res * eps) + self.num_final]
        sampled = random.sample(list(indices.cpu().numpy()), self.num_sampled)
        sampled = sorted(sampled)
        print(f"all path: {key}")
        print(f"sampled: {sampled}")
        path = [key[i] for i in range(len(key)) if i in sampled]
        print(f"path: {path}")
        return sampled
    

    def forward(self, epoch_sampled, feats_dict, label_feats_dict, label_emb):

        all_meta_path = list(feats_dict.keys()) + list(label_feats_dict.keys())

        meta_path_sampled = [all_meta_path[i] for i in range(self.num_feats) if i in epoch_sampled]
        label_meta_path_sampled = [all_meta_path[i] for i in range(self.num_feats, self.num_paths) if i in epoch_sampled]

        for k, v in feats_dict.items():
            if k in self.embeding and k in meta_path_sampled:
                feats_dict[k] = self.input_drop(v @ self.embeding[k])
        
        for k, v in label_feats_dict.items():
            if k in self.labels_embeding and k in label_meta_path_sampled:
                label_feats_dict[k] = self.input_drop(v @ self.labels_embeding[k])

            
        x = [feats_dict[k] for k in meta_path_sampled] + [label_feats_dict[k] for k in label_meta_path_sampled]
        x = torch.stack(x, dim=1)

        ws = [self.alpha[idx] for idx in epoch_sampled]

        def get_gumbel_prob(xins):
            while True:
                gumbels = -torch.empty_like(xins).exponential_().log()
                logits = (xins.log_softmax(dim=-1) + gumbels) / self.tau
                probs = nn.functional.softmax(logits, dim=-1)
                index = probs.max(-1, keepdim=True)[-1]
                one_h = torch.zeros_like(logits).scatter_(-1, index, 1.0)
                if (
                        (torch.isinf(gumbels).any())
                        or (torch.isinf(probs).any())
                        or (torch.isnan(probs).any())
                ):
                    continue
                else:
                    break
            return probs
        ws = get_gumbel_prob(torch.stack(ws))


        x = torch.einsum('bcd,c->bd', x, ws)

        if self.residual:
            k = self.tgt_key
            if k not in meta_path_sampled:
                tgt_feat = self.input_drop(feats_dict[k] @ self.embeding[k])
            else:
                tgt_feat = feats_dict[k]
            x = x + self.res_fc(tgt_feat)

        x = self.dropout(self.prelu(x))
        x = self.lr_output(x)
        
        if self.label_residual:
            x = x + self.label_res_fc(self.label_drop(label_emb))

        return x


    def set_tau(self, tau):
        self.tau = tau

    def sample(self, keys, label_keys, lam, topn, all_path=False):
        '''
        to sample one candidate edge type per link
        '''
        length = len(self.alpha)
        seq_softmax = None if self.alpha is None else F.softmax(self.alpha, dim=-1)
        max = torch.max(seq_softmax, dim=0).values
        min = torch.min(seq_softmax, dim=0).values
        threshold = lam * max + (1 - lam) * min


        _, idxl = torch.sort(seq_softmax, descending=True)


        idx = idxl[:self.num_sampled]

        if all_path:
            path = []
            label_path = []
            for i, index in enumerate(idxl):
                if index < len(keys):
                    path.append((keys[index], i))
                else:
                    label_path.append((label_keys[index - len(keys)], i))
            return [path, label_path], idx

        if topn:
            id_paths = idxl[:topn]
        else:
            id_paths = [k for k in range(length) if seq_softmax[k].item() >= threshold]
        path = [keys[i] for i in range(len(keys)) if i in id_paths]
        label_path = [label_keys[i] for i in range(len(label_keys)) if i+len(keys) in id_paths]
        return [path, label_path], idx