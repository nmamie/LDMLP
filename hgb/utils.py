import sys
import gc
import random

import dgl
import dgl.function as fn

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_sparse import SparseTensor

import numpy as np
from sklearn.metrics import f1_score
from data_loader import data_loader

sys.path.append('../data')

import warnings
warnings.filterwarnings("ignore", message="Setting attributes on ParameterList is not supported.")


def set_random_seed(seed=0):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)


def evaluator(gt, pred):
    gt = gt.cpu().squeeze()
    pred = pred.cpu().squeeze()
    return f1_score(gt, pred, average='micro'), f1_score(gt, pred, average='macro')


def get_n_params(model):
    pp = 0
    for p in list(model.parameters()):
        nn = 1
        for s in list(p.size()):
            nn = nn*s
        pp += nn
    return pp


def hg_propagate_feat_dgl(g, tgt_type, num_hops, max_length, echo=False):
    for hop in range(1, max_length):
        #reserve_heads = [ele[:hop] for ele in extra_metapath if len(ele) > hop]

        for etype in g.etypes:
            stype, _, dtype = g.to_canonical_etype(etype)
            # if hop == args.num_hops and dtype != tgt_type: continue
            for k in list(g.nodes[stype].data.keys()):
                if len(k) == hop:
                    current_dst_name = f'{dtype}{k}'
                    if (hop == num_hops and dtype != tgt_type ) \
                      or (hop > num_hops):
                        continue
                    if echo: print(k, etype, current_dst_name)
                    g[etype].update_all(
                        fn.copy_u(k, 'm'),
                        fn.mean('m', current_dst_name), etype=etype)

        # remove no-use items
        for ntype in g.ntypes:
            if ntype == tgt_type: continue
            removes = []
            for k in g.nodes[ntype].data.keys():
                if len(k) <= hop:
                    removes.append(k)
            for k in removes:
                g.nodes[ntype].data.pop(k)
            if echo and len(removes): print('remove', removes)
        gc.collect()

        if echo: print(f'-- hop={hop} ---')
        for ntype in g.ntypes:
            for k, v in g.nodes[ntype].data.items():
                print(f'{ntype} {k} {v.shape}', v[:,-1].max(), v[:,-1].mean())

        if echo: print(f'------\n')

    return g


def hg_propagate_feat_dgl_path(g, tgt_type, num_hops, max_length, meta_path, echo=False):
    for hop in range(1, max_length):
        #reserve_heads = [ele[:hop] for ele in extra_metapath if len(ele) > hop]

        for etype in g.etypes:
            stype, _, dtype = g.to_canonical_etype(etype)

            for k in list(g.nodes[stype].data.keys()):
                if len(k) == hop:
                    current_dst_name = f'{dtype}{k}'
                    if (hop == num_hops and dtype != tgt_type ) \
                      or (hop > num_hops):
                        continue
                    if echo: print(k, etype, current_dst_name)
                    g[etype].update_all(
                        fn.copy_u(k, 'm'),
                        fn.mean('m', current_dst_name), etype=etype)

        # remove no-use items
        for ntype in g.ntypes:

            if ntype == tgt_type: continue
            removes = []
            for k in g.nodes[ntype].data.keys():

                if len(k) <= hop:
                    removes.append(k)
            for k in removes:
                g.nodes[ntype].data.pop(k)
            if echo and len(removes): print('remove', removes)
        gc.collect()

        if echo:
            print(f'-- hop={hop} ---')
            for ntype in g.ntypes:
                for k, v in g.nodes[ntype].data.items():
                    print(f'{ntype} {k} {v.shape}', v[:,-1].max(), v[:,-1].mean())

        if echo: print(f'------\n')
    return g


def hg_propagate_sparse_pyg(adjs, tgt_types, num_hops, max_length, extra_metapath, prop_feats=False, echo=False, prop_device='cpu'):
    store_device = 'cpu'
    if type(tgt_types) is not list:
        tgt_types = [tgt_types]

    label_feats = {k: v.clone() for k, v in adjs.items() if prop_feats or k[-1] in tgt_types} # metapath should start with target type in label propagation
    adjs_g = {k: v.to(prop_device) for k, v in adjs.items()}

    for hop in range(2, max_length):
        reserve_heads = [ele[-(hop+1):] for ele in extra_metapath if len(ele) > hop]
        new_adjs = {}
        for rtype_r, adj_r in label_feats.items():
            metapath_types = list(rtype_r)
            if len(metapath_types) == hop:
                dtype_r, stype_r = metapath_types[0], metapath_types[-1]
                for rtype_l, adj_l in adjs_g.items():
                    dtype_l, stype_l = rtype_l
                    if stype_l == dtype_r:
                        name = f'{dtype_l}{rtype_r}'
                        if (hop == num_hops and dtype_l not in tgt_types and name not in reserve_heads) \
                          or (hop > num_hops and name not in reserve_heads):
                            continue
                        if name not in new_adjs:
                            if echo: print('Generating ...', name)
                            if prop_device == 'cpu':
                                new_adjs[name] = adj_l.matmul(adj_r)
                            else:
                                with torch.no_grad():
                                    new_adjs[name] = adj_l.matmul(adj_r.to(prop_device)).to(store_device)
                        else:
                            if echo: print(f'Warning: {name} already exists')
        label_feats.update(new_adjs)

        removes = []
        for k in label_feats.keys():
            metapath_types = list(k)
            if metapath_types[0] in tgt_types: continue  # metapath should end with target type in label propagation
            if len(metapath_types) <= hop:
                removes.append(k)
        for k in removes:
            label_feats.pop(k)
        if echo and len(removes): print('remove', removes)
        del new_adjs
        gc.collect()

    if prop_device != 'cpu':
        del adjs_g
        torch.cuda.empty_cache()

    return label_feats


def check_acc(preds_dict, condition, init_labels, train_nid, val_nid, test_nid, show_test=True, loss_type='ce'):
    mask_train, mask_val, mask_test = [], [], []
    remove_label_keys = []
    k = list(preds_dict.keys())[0]
    v = preds_dict[k]
    if loss_type == 'ce':
        na, nb, nc = len(train_nid), len(val_nid), len(test_nid)
    elif loss_type == 'bce':
        na, nb, nc = len(train_nid) * v.size(1), len(val_nid) * v.size(1), len(test_nid) * v.size(1)

    for k, v in preds_dict.items():
        if loss_type == 'ce':
            pred = v.argmax(1)
        elif loss_type == 'bce':
            pred = (v > 0).int()

        a, b, c = pred[train_nid] == init_labels[train_nid], \
                  pred[val_nid] == init_labels[val_nid], \
                  pred[test_nid] == init_labels[test_nid]
        ra, rb, rc = a.sum() / na, b.sum() / nb, c.sum() / nc

        if loss_type == 'ce':
            vv = torch.log(v / (v.sum(1, keepdim=True) + 1e-6) + 1e-6)
            la, lb, lc = F.nll_loss(vv[train_nid], init_labels[train_nid]), \
                         F.nll_loss(vv[val_nid], init_labels[val_nid]), \
                         F.nll_loss(vv[test_nid], init_labels[test_nid])
        else:
            vv = (v / 2. + 0.5).clamp(1e-6, 1-1e-6)
            la, lb, lc = F.binary_cross_entropy(vv[train_nid], init_labels[train_nid].float()), \
                         F.binary_cross_entropy(vv[val_nid], init_labels[val_nid].float()), \
                         F.binary_cross_entropy(vv[test_nid], init_labels[test_nid].float())
        if condition(ra, rb, rc, k):
            mask_train.append(a)
            mask_val.append(b)
            mask_test.append(c)
        else:
            remove_label_keys.append(k)

    print(set(list(preds_dict.keys())) - set(remove_label_keys))

    print((torch.stack(mask_train, dim=0).sum(0) > 0).sum() / na)
    print((torch.stack(mask_val, dim=0).sum(0) > 0).sum() / nb)
    if show_test:
        print((torch.stack(mask_test, dim=0).sum(0) > 0).sum() / nc)


def train_multi_stage(model, feats, label_feats, labels_cuda, loss_fcn, optimizer, train_loader, enhance_loader, evaluator, predict_prob, gama, mask=None, scalar=None):
    model.train()
    device = labels_cuda.device
    total_loss = 0
    loss_l1, loss_l2 = 0., 0.
    iter_num = 0
    y_true, y_pred = [], []

    for idx_1, idx_2 in zip(train_loader, enhance_loader):
        idx = torch.cat((idx_1, idx_2), dim=0)
        L1_ratio = len(idx_1) * 1.0 / (len(idx_1) + len(idx_2))
        L2_ratio = len(idx_2) * 1.0 / (len(idx_1) + len(idx_2))

        if isinstance(feats, list):
            batch_feats = [x[idx].to(device) for x in feats]
        elif isinstance(feats, dict):
            batch_feats = {k: x[idx].to(device) for k, x in feats.items()}
        else:
            assert 0
        batch_labels_feats = {k: x[idx].to(device) for k, x in label_feats.items()}
        if mask is not None:
            batch_mask = {k: x[idx].to(device) for k, x in mask.items()}
        else:
            batch_mask = None
        batch_y = labels_cuda[idx_1]
        if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
            extra_weight = 2 * torch.abs(predict_prob[idx_2] - 0.5)
            extra_y = (predict_prob[idx_2] > 0.5).float()
        else:
            extra_weight, extra_y = predict_prob[idx_2].max(dim=1)
        extra_weight = extra_weight.to(device)
        extra_y = extra_y.to(device)

        optimizer.zero_grad()
        if scalar is not None:
            with torch.cuda.amp.autocast():
                output_att = model(None, batch_feats, batch_labels_feats, batch_mask)
                L1 = loss_fcn(output_att[:len(idx_1)], batch_y)
                if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
                    L2 = F.binary_cross_entropy_with_logits(output_att[len(idx_1):], extra_y, reduction='none')
                else:
                    L2 = F.cross_entropy(output_att[len(idx_1):], extra_y, reduction='none')
                L2 = (L2 * extra_weight).sum() / len(idx_2)
                loss_train = L1_ratio * L1 + gama * L2_ratio * L2
            scalar.scale(loss_train).backward()
            scalar.step(optimizer)
            scalar.update()
        else:
            output_att = model(None, batch_feats, batch_labels_feats, batch_mask)
            L1 = loss_fcn(output_att[:len(idx_1)], batch_y)
            if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
                L2 = F.binary_cross_entropy_with_logits(output_att[len(idx_1):], extra_y, reduction='none')
            else:
                L2 = F.cross_entropy(output_att[len(idx_1):], extra_y, reduction='none')
            L2 = (L2 * extra_weight).sum() / len(idx_2)
            loss_train = L1_ratio * L1 + gama * L2_ratio * L2
            loss_train.backward()
            optimizer.step()

        y_true.append(batch_y.cpu().to(torch.long))
        if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
            y_pred.append((output_att[:len(idx_1)].data.cpu() > 0.).int())
        else:
            y_pred.append(output_att[:len(idx_1)].argmax(dim=-1, keepdim=True).cpu())
        total_loss += loss_train.item()
        loss_l1 += L1.item()
        loss_l2 += L2.item()
        iter_num += 1

    print(loss_l1 / iter_num, loss_l2 / iter_num)
    loss = total_loss / iter_num
    approx_acc = evaluator(torch.cat(y_true, dim=0), torch.cat(y_pred, dim=0))
    return loss, approx_acc


def train(model, feats, label_feats, labels_cuda, loss_fcn, optimizer, train_loader, evaluator, mask=None, scalar=None):
    model.train()
    device = labels_cuda.device
    total_loss = 0
    iter_num = 0
    y_true, y_pred = [], []

    for batch in train_loader:
        batch = batch.to(device)
        if isinstance(feats, list):
            batch_feats = [x[batch].to(device) for x in feats]
        elif isinstance(feats, dict):
            batch_feats = {k: x[batch].to(device) for k, x in feats.items()}
        else:
            assert 0
        batch_labels_feats = {k: x[batch].to(device) for k, x in label_feats.items()}
        if mask is not None:
            batch_mask = {k: x[batch].to(device) for k, x in mask.items()}
        else:
            batch_mask = None
        batch_y = labels_cuda[batch]

        optimizer.zero_grad()
        if scalar is not None:
            with torch.cuda.amp.autocast():
                output_att = model(batch, batch_feats, batch_labels_feats, batch_mask)
                loss_train = loss_fcn(output_att, batch_y)
            scalar.scale(loss_train).backward()
            scalar.step(optimizer)
            scalar.update()
        else:
            output_att = model(batch, batch_feats, batch_labels_feats, batch_mask)
            L1 = loss_fcn(output_att, batch_y)
            loss_train = L1
            loss_train.backward()
            optimizer.step()

        y_true.append(batch_y.cpu().to(torch.long))
        if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
            y_pred.append((output_att.data.cpu() > 0.).int())
        else:
            y_pred.append(output_att.argmax(dim=-1, keepdim=True).cpu())
        total_loss += loss_train.item()
        iter_num += 1
    loss = total_loss / iter_num
    acc = evaluator(torch.cat(y_true, dim=0), torch.cat(y_pred, dim=0))
    return loss, acc



def train_search(model, feats, label_feats, labels_cuda, loss_fcn, optimizer_w, optimizer_a, train_loader, val_loader, epoch_sampled, evaluator, mask=None, scalar=None):
    model.train()
    device = labels_cuda.device
    total_loss = 0
    iter_num = 0
    y_true, y_pred = [], []
    val_total_loss = 0
    val_y_true, val_y_pred = [], []
    ###################  optimize w  ##################
    for batch in train_loader:
        batch = batch.to(device)
        val_batch = next(iter(val_loader)).to(device)
        if isinstance(feats, list):
            batch_feats = [x[batch].to(device) for x in feats]
            val_batch_feats = [x[val_batch].to(device) for x in feats]
        elif isinstance(feats, dict):
            batch_feats = {k: x[batch].to(device) for k, x in feats.items()}
            val_batch_feats = {k: x[val_batch].to(device) for k, x in feats.items()}
        else:
            assert 0
        batch_labels_feats = {k: x[batch].to(device) for k, x in label_feats.items()}
        val_batch_labels_feats = {k: x[val_batch].to(device) for k, x in label_feats.items()}
        if mask is not None:
            batch_mask = {k: x[batch].to(device) for k, x in mask.items()}
            val_batch_mask = {k: x[val_batch].to(device) for k, x in mask.items()}
        else:
            batch_mask = None
            val_batch_mask = None
        batch_y = labels_cuda[batch]
        val_batch_y = labels_cuda[val_batch]

        ########################################train
        optimizer_w.zero_grad()
        if scalar is not None:
            with torch.cuda.amp.autocast():
                output_att = model(batch, batch_feats, epoch_sampled, batch_labels_feats, batch_mask)
                loss_train = loss_fcn(output_att, batch_y)
            scalar.scale(loss_train).backward(retain_graph=True)
            scalar.step(optimizer_w)
            scalar.update()
        else:
            output_att = model(batch, batch_feats, epoch_sampled, batch_labels_feats, batch_mask)
            L1 = loss_fcn(output_att, batch_y)
            loss_train = L1
            loss_train.backward(retain_graph=True)
            optimizer_w.step()

        ########################################val  update a
        optimizer_a.zero_grad()
        if scalar is not None:
            with torch.cuda.amp.autocast():
                val_output_att = model(val_batch, val_batch_feats, epoch_sampled, val_batch_labels_feats, val_batch_mask)
                val_loss_train = loss_fcn(val_output_att, val_batch_y)
            scalar.scale(val_loss_train).backward()
            scalar.step(optimizer_a)
            scalar.update()
        else:
            val_output_att = model(val_batch, val_batch_feats, epoch_sampled, val_batch_labels_feats, val_batch_mask)
            L1 = loss_fcn(val_output_att, val_batch_y)
            val_loss_train = L1
            val_loss_train.backward()
            optimizer_a.step()

        ########################################
        y_true.append(batch_y.cpu().to(torch.long))
        val_y_true.append(val_batch_y.cpu().to(torch.long))
        if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
            y_pred.append((output_att.data.cpu() > 0.).int())
            val_y_pred.append((val_output_att.data.cpu() > 0.).int())
        else:
            y_pred.append(output_att.argmax(dim=-1, keepdim=True).cpu())
            val_y_pred.append(val_output_att.argmax(dim=-1, keepdim=True).cpu())
        total_loss += loss_train.item()
        val_total_loss += val_loss_train.item()
        iter_num += 1
        # import code
        # code.interact(local=locals())

    loss_train = total_loss / iter_num
    acc_train = evaluator(torch.cat(y_true, dim=0), torch.cat(y_pred, dim=0))
    loss_val = val_total_loss / iter_num
    acc_val = evaluator(torch.cat(val_y_true, dim=0), torch.cat(val_y_pred, dim=0))

    return loss_train, loss_val, acc_train, acc_val


def train_search_new(model, feats, label_feats, labels_cuda, loss_fcn, optimizer_w, optimizer_a, train_loader, val_loader, epoch_sampled, meta_path_sampled, label_meta_path_sampled, evaluator, mask=None, scalar=None):
    model.train()
    device = labels_cuda.device
    total_loss = 0
    iter_num = 0
    y_true, y_pred = [], []
    val_total_loss = 0
    val_y_true, val_y_pred = [], []
    ###################  optimize w  ##################
    for batch in train_loader:
        batch = batch.to(device)
        val_batch = next(iter(val_loader)).to(device)
        if isinstance(feats, list):
            batch_feats = [x[batch].to(device) for x in feats]
            val_batch_feats = [x[val_batch].to(device) for x in feats]
        elif isinstance(feats, dict):
            batch_feats = {k: x[batch].to(device) for k, x in feats.items()}
            val_batch_feats = {k: x[val_batch].to(device) for k, x in feats.items()}
        else:
            assert 0
        batch_labels_feats = {k: x[batch].to(device) for k, x in label_feats.items()}
        val_batch_labels_feats = {k: x[val_batch].to(device) for k, x in label_feats.items()}
        if mask is not None:
            batch_mask = {k: x[batch].to(device) for k, x in mask.items()}
            val_batch_mask = {k: x[val_batch].to(device) for k, x in mask.items()}
        else:
            batch_mask = None
            val_batch_mask = None
        batch_y = labels_cuda[batch]
        val_batch_y = labels_cuda[val_batch]

        ########################################train
        optimizer_w.zero_grad()
        if scalar is not None:
            with torch.cuda.amp.autocast():
                output_att = model(epoch_sampled, batch_feats, batch_labels_feats, meta_path_sampled, label_meta_path_sampled)
                loss_train = loss_fcn(output_att, batch_y)
            scalar.scale(loss_train).backward(retain_graph=True)
            scalar.step(optimizer_w)
            scalar.update()
        else:
            output_att = model(epoch_sampled, batch_feats, batch_labels_feats, meta_path_sampled, label_meta_path_sampled)
            L1 = loss_fcn(output_att, batch_y)
            loss_train = L1
            loss_train.backward(retain_graph=True)
            optimizer_w.step()

        ########################################val  update a
        optimizer_a.zero_grad()
        if scalar is not None:
            with torch.cuda.amp.autocast():
                val_output_att = model(epoch_sampled, val_batch_feats, val_batch_labels_feats, meta_path_sampled, label_meta_path_sampled)
                val_loss_train = loss_fcn(val_output_att, val_batch_y)
            scalar.scale(val_loss_train).backward()
            scalar.step(optimizer_a)
            scalar.update()
        else:
            val_output_att = model(epoch_sampled, val_batch_feats, val_batch_labels_feats, meta_path_sampled, label_meta_path_sampled)
            L1 = loss_fcn(val_output_att, val_batch_y)
            val_loss_train = L1
            val_loss_train.backward()
            optimizer_a.step()

        ########################################
        y_true.append(batch_y.cpu().to(torch.long))
        val_y_true.append(val_batch_y.cpu().to(torch.long))
        if isinstance(loss_fcn, nn.BCEWithLogitsLoss):
            y_pred.append((output_att.data.cpu() > 0.).int())
            val_y_pred.append((val_output_att.data.cpu() > 0.).int())
        else:
            y_pred.append(output_att.argmax(dim=-1, keepdim=True).cpu())
            val_y_pred.append(val_output_att.argmax(dim=-1, keepdim=True).cpu())
        total_loss += loss_train.item()
        val_total_loss += val_loss_train.item()
        iter_num += 1
        # import code
        # code.interact(local=locals())

    loss_train = total_loss / iter_num
    acc_train = evaluator(torch.cat(y_true, dim=0), torch.cat(y_pred, dim=0))
    loss_val = val_total_loss / iter_num
    acc_val = evaluator(torch.cat(val_y_true, dim=0), torch.cat(val_y_pred, dim=0))

    return loss_train, loss_val, acc_train, acc_val


def load_dataset(args):
    dl = data_loader(f'{args.root}/{args.dataset}')

    # use one-hot index vectors for nods with no attributes
    # === feats ===
    features_list = []
    for i in range(len(dl.nodes['count'])):
        th = dl.nodes['attr'][i]
        if th is None:
            features_list.append(torch.eye(dl.nodes['count'][i]))
        else:
            features_list.append(torch.FloatTensor(th))

    idx_shift = np.zeros(len(dl.nodes['count'])+1, dtype=np.int32)
    for i in range(len(dl.nodes['count'])):
        idx_shift[i+1] = idx_shift[i] + dl.nodes['count'][i]

    # === labels ===
    num_classes = dl.labels_train['num_classes']
    init_labels = np.zeros((dl.nodes['count'][0], num_classes), dtype=int)

    val_ratio = 0.2
    train_nid = np.nonzero(dl.labels_train['mask'])[0]
    np.random.shuffle(train_nid)
    split = int(train_nid.shape[0]*val_ratio)
    val_nid = train_nid[:split]
    train_nid = train_nid[split:]
    train_nid = np.sort(train_nid)
    val_nid = np.sort(val_nid)
    test_nid = np.nonzero(dl.labels_test['mask'])[0]
    test_nid_full = np.nonzero(dl.labels_test_full['mask'])[0]

    init_labels[train_nid] = dl.labels_train['data'][train_nid]
    init_labels[val_nid] = dl.labels_train['data'][val_nid]
    init_labels[test_nid] = dl.labels_test['data'][test_nid]
    if args.dataset != 'IMDB':
        init_labels = init_labels.argmax(axis=1)

    print(len(train_nid), len(val_nid), len(test_nid), len(test_nid_full))
    init_labels = torch.LongTensor(init_labels)

    # === adjs ===
    # print(dl.nodes['attr'])
    # for k, v in dl.nodes['attr'].items():
    #     if v is None: print('none')
    #     else: print(v.shape)
    adjs = []
    for i, (k, v) in enumerate(dl.links['data'].items()):
        v = v.tocoo()
        src_type_idx = np.where(idx_shift > v.col[0])[0][0] - 1
        dst_type_idx = np.where(idx_shift > v.row[0])[0][0] - 1
        row = v.row - idx_shift[dst_type_idx]
        col = v.col - idx_shift[src_type_idx]
        sparse_sizes = (dl.nodes['count'][dst_type_idx], dl.nodes['count'][src_type_idx])
        adj = SparseTensor(row=torch.LongTensor(row), col=torch.LongTensor(col), sparse_sizes=sparse_sizes)

        adjs.append(adj)
            #print(adj)


    if args.dataset == 'DBLP':
        # A* --- P --- T
        #        |
        #        V
        # author: [4057, 334]
        # paper : [14328, 4231]
        # term  : [7723, 50]
        # venue(conference) : None
        A, P, T, V = features_list
        AP, PA, PT, PV, TP, VP = adjs

        new_edges = {}
        ntypes = set()
        etypes = [ # src->tgt
            ('P', 'P-A', 'A'),
            ('A', 'A-P', 'P'),
            ('T', 'T-P', 'P'),
            ('V', 'V-P', 'P'),
            ('P', 'P-T', 'T'),
            ('P', 'P-V', 'V'),
        ]
    
        
        for etype, adj in zip(etypes, adjs):
            stype, rtype, dtype = etype
            dst, src, _ = adj.coo()
            src = src.numpy()
            dst = dst.numpy()
            new_edges[(stype, rtype, dtype)] = (src, dst)
            ntypes.add(stype)
            ntypes.add(dtype)
        g = dgl.heterograph(new_edges)

        # for i, etype in enumerate(g.etypes):
        #     src, dst, eid = g._graph.edges(i)
        #     adj = SparseTensor(row=dst.long(), col=src.long())
        #     print(etype, adj)

        # g.ndata['feat']['A'] = A # not work
        g.nodes['A'].data['A'] = A
        g.nodes['P'].data['P'] = P
        g.nodes['T'].data['T'] = T
        g.nodes['V'].data['V'] = V
    elif args.dataset == 'IMDB':
        # A --- M* --- D
        #       |
        #       K
        # movie    : [4932, 3489]
        # director : [2393, 3341]
        # actor    : [6124, 3341]
        # keywords : None
        M, D, A, K = features_list
        MD, DM, MA, AM, MK, KM = adjs
        assert torch.all(DM.storage.col() == MD.t().storage.col())
        assert torch.all(AM.storage.col() == MA.t().storage.col())
        assert torch.all(KM.storage.col() == MK.t().storage.col())

        assert torch.all(MD.storage.rowcount() == 1) # each movie has single director

        new_edges = {}
        ntypes = set()
        etypes = [ # src->tgt
            ('D', 'D-M', 'M'),
            ('M', 'M-D', 'D'),
            ('A', 'A-M', 'M'),
            ('M', 'M-A', 'A'),
            ('K', 'K-M', 'M'),
            ('M', 'M-K', 'K'),
        ]

        for etype, adj in zip(etypes, adjs):
            stype, rtype, dtype = etype
            dst, src, _ = adj.coo()
            src = src.numpy()
            dst = dst.numpy()
            new_edges[(stype, rtype, dtype)] = (src, dst)
            ntypes.add(stype)
            ntypes.add(dtype)
        g = dgl.heterograph(new_edges)

        g.nodes['M'].data['M'] = M
        g.nodes['D'].data['D'] = D
        g.nodes['A'].data['A'] = A
        if args.num_hops > 2 :#or args.two_layer:
            g.nodes['K'].data['K'] = K
    elif args.dataset == 'ACM':
        # A --- P* --- C
        #       |
        #       K
        # paper     : [3025, 1902]
        # author    : [5959, 1902]
        # conference: [56, 1902]
        # field     : None
        P, A, C, K = features_list
        PP, PP_r, PA, AP, PC, CP, PK, KP = adjs
        row, col = torch.where(P)
        assert torch.all(row == PK.storage.row()) and torch.all(col == PK.storage.col())
        assert torch.all(AP.matmul(PK).to_dense() == A)
        assert torch.all(CP.matmul(PK).to_dense() == C)

        assert torch.all(PA.storage.col() == AP.t().storage.col())
        assert torch.all(PC.storage.col() == CP.t().storage.col())
        assert torch.all(PK.storage.col() == KP.t().storage.col())

        row0, col0, _ = PP.coo()
        row1, col1, _ = PP_r.coo()
        PP = SparseTensor(row=torch.cat((row0, row1)), col=torch.cat((col0, col1)), sparse_sizes=PP.sparse_sizes())
        PP = PP.coalesce()
        PP = PP.set_diag()
        adjs = [PP] + adjs[2:]

        new_edges = {}
        ntypes = set()
        etypes = [ # src->tgt
            ('P', 'P-P', 'P'),
            ('A', 'A-P', 'P'),
            ('P', 'P-A', 'A'),
            ('C', 'C-P', 'P'),
            ('P', 'P-C', 'C'),
        ]


        if args.ACM_keep_F:
            etypes += [
                ('K', 'K-P', 'P'),
                ('P', 'P-K', 'K'),
            ]
        for etype, adj in zip(etypes, adjs):
            stype, rtype, dtype = etype
            dst, src, _ = adj.coo()
            src = src.numpy()
            dst = dst.numpy()
            new_edges[(stype, rtype, dtype)] = (src, dst)
            ntypes.add(stype)
            ntypes.add(dtype)

        g = dgl.heterograph(new_edges)

        g.nodes['P'].data['P'] = P # [3025, 1902]
        g.nodes['A'].data['A'] = A # [5959, 1902]
        g.nodes['C'].data['C'] = C # [56, 1902]
        if args.ACM_keep_F:
            g.nodes['K'].data['K'] = K # [1902, 1902]
    else:
        assert 0

    if args.dataset == 'DBLP':
        adjs = {'AP': AP, 'PA': PA, 'PT': PT, 'PV': PV, 'TP': TP, 'VP': VP}
    elif args.dataset == 'ACM':
        adjs = {'PP': PP, 'PA': PA, 'AP': AP, 'PC': PC, 'CP': CP}
    elif args.dataset == 'IMDB':
        adjs = {'MD': MD, 'DM': DM, 'MA': MA, 'AM': AM, 'MK': MK, 'KM': KM}
    else:
        assert 0

    return g, adjs, init_labels, num_classes, dl, train_nid, val_nid, test_nid, test_nid_full


class EarlyStopping:
    def __init__(self, patience, verbose=False, delta=0, save_path='checkpoint.pt'):
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta
        self.save_path = save_path

    def __call__(self, val_loss, model):

        score = -val_loss

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
        elif score < self.best_score - self.delta:
            self.counter += 1
            print(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
            self.counter = 0

    def save_checkpoint(self, val_loss, model):
        if self.verbose:
            print(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
        torch.save(model.state_dict(), self.save_path)
        self.val_loss_min = val_loss
