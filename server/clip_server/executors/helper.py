from typing import Tuple, List, Callable, Any, Dict, Union
import torch
import numpy as np
from docarray import Document, DocumentArray
from docarray.math.distance.numpy import cosine
from clip_server.helper import __cast_dtype__


from clip_server.model.tokenization import Tokenizer


def numpy_softmax(x: 'np.ndarray', axis: int = -1) -> 'np.ndarray':
    max = np.max(x, axis=axis, keepdims=True)
    e_x = np.exp(x - max)
    div = np.sum(e_x, axis=axis, keepdims=True)
    return e_x / div


def preproc_image(
    da: 'DocumentArray',
    preprocess_fn: Callable,
    device: str = 'cpu',
    return_np: bool = False,
    drop_image_content: bool = False,
    dtype: Union[str, torch.dtype] = torch.float32,
) -> Tuple['DocumentArray', Dict]:

    if isinstance(dtype, str):
        dtype = __cast_dtype__.get(dtype)

    tensors_batch = []

    for d in da:
        content = d.content
        if d.tensor is not None:
            d.convert_image_tensor_to_blob()
        elif d.content_type != 'blob' and d.uri:
            # in case user uses HTTP protocol and send data via curl not using .blob (base64), but in .uri
            d.load_uri_to_blob()

        tensors_batch.append(preprocess_fn(d.blob).detach())

        # recover doc content
        d.content = content
        if drop_image_content:
            d.pop('blob', 'tensor')

    tensors_batch = torch.stack(tensors_batch).type(dtype)

    if return_np:
        tensors_batch = tensors_batch.cpu().numpy()
    else:
        tensors_batch = tensors_batch.to(device)

    return da, {'pixel_values': tensors_batch}


def preproc_text(
    da: 'DocumentArray',
    tokenizer: 'Tokenizer',
    device: str = 'cpu',
    return_np: bool = False,
) -> Tuple['DocumentArray', Dict]:

    inputs = tokenizer(da.texts)
    inputs['input_ids'] = inputs['input_ids'].detach()

    if return_np:
        inputs['input_ids'] = inputs['input_ids'].cpu().numpy().astype(np.int32)
        inputs['attention_mask'] = (
            inputs['attention_mask'].cpu().numpy().astype(np.int32)
        )
    else:
        inputs['input_ids'] = inputs['input_ids'].to(device)
        inputs['attention_mask'] = inputs['attention_mask'].to(device)

    da[:, 'mime_type'] = 'text'
    return da, inputs


def split_img_txt_da(doc: 'Document', img_da: 'DocumentArray', txt_da: 'DocumentArray'):
    if doc.text:
        txt_da.append(doc)
    elif doc.blob or (doc.tensor is not None) or doc.uri:
        img_da.append(doc)


def set_rank(docs, _logit_scale=np.exp(4.60517)):
    queries = docs
    candidates = docs['@m']

    query_embeddings = queries.embeddings  # Q X D
    candidate_embeddings = candidates.embeddings  # C = Sum(C_q1, C_q2, C_q3,...) x D
    cosine_scores = 1 - cosine(
        query_embeddings, candidate_embeddings
    )  # Q x C Block matix
    start_idx = 0
    for q, _cosine_scores in zip(docs, cosine_scores):

        _candidates = q.matches

        end_idx = start_idx + len(_candidates)

        _candidate_cosines = _cosine_scores[start_idx:end_idx]
        _candidate_softmaxs = numpy_softmax(_logit_scale * _candidate_cosines)
        for c, _c_score, _s_score in zip(
            _candidates, _candidate_cosines, _candidate_softmaxs
        ):
            c.scores['clip_score'].value = _s_score
            c.scores['clip_score'].op_name = 'softmax'

            c.scores['clip_score_cosine'].value = _c_score
            c.scores['clip_score_cosine'].op_name = 'cosine'

        start_idx = end_idx

        _candidates.embeddings = None  # remove embedding to save bandwidth

        final = sorted(
            _candidates, key=lambda _m: _m.scores['clip_score'].value, reverse=True
        )

        q.matches = final


def get_image_size(name: str):
    from clip_server.model.pretrained_models import _VISUAL_MODEL_IMAGE_SIZE

    return _VISUAL_MODEL_IMAGE_SIZE[name]
