"""Threshold proposals evaluated on a deterministic holdout of human labels."""
import hashlib

from .audit import normalize_username
from .evaluation import _labels, context_hash
from .repository import content_hash


def scores(points, threshold):
    tp = fp = fn = tn = 0
    for ratio, actual in points:
        predicted = ratio >= threshold
        tp += bool(predicted and actual)
        fp += bool(predicted and not actual)
        fn += bool(not predicted and actual)
        tn += bool(not predicted and not actual)
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn, 'precision': round(precision, 3),
            'recall': round(recall, 3), 'f1': round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0}


def propose_threshold(repo):
    labels = _labels(repo)
    points = []
    for row in repo.list_channels():
        user = normalize_username(row['username'])
        label = labels.get((user, content_hash(row), context_hash(row)))
        ratio = row.get('activity_ratio')
        if label and type(ratio) in (float, int):
            points.append((hashlib.sha256(user.encode()).hexdigest(), ratio, label['qualified']))
    if len(points) < 20:
        return {'ready': False, 'reason': f'Нужно минимум 20 актуальных размеченных каналов с замером; сейчас {len(points)}'}
    points.sort()
    train = [(r, a) for i, (_, r, a) in enumerate(points) if i % 5]
    holdout = [(r, a) for i, (_, r, a) in enumerate(points) if not i % 5]
    if any(len({a for _, a in sample}) < 2 for sample in (train, holdout)):
        return {'ready': False, 'reason': 'В обучении и контрольной части нужны и подходящие, и неподходящие каналы'}
    threshold = max((i / 2 for i in range(1, 101)), key=lambda t: (scores(train, t)['f1'], scores(train, t)['precision'], -abs(t - 10)))
    return {'ready': True, 'threshold': threshold, 'train_count': len(train), 'holdout_count': len(holdout),
            'train': scores(train, threshold), 'holdout': scores(holdout, threshold),
            'baseline_holdout': scores(holdout, 10), 'note': 'Диагностический одномерный порог; классификация автора и темы остаётся обязательной. Не гарантия качества на новых каналах.'}
