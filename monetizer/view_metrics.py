"""Subscriber activity measured on the post closest to the selected age."""
from datetime import datetime, timezone

METRIC_VERSION = 'post-activity-1'


def _date(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def assess_post_views(row, settings):
    """Choose by distance from the target age, never by a better view count."""
    candidates, failures, uncertain = [], [], []
    target, tolerance = settings.target_post_age_hours, settings.post_age_tolerance_hours
    lower, upper = target - tolerance, target + tolerance
    observations = row.get('post_observations') or []
    if not isinstance(observations, (list, tuple)):
        observations = []
    for post in observations:
        if not isinstance(post, dict) or not isinstance(post.get('id'), str) or not post['id']:
            continue
        published, observed = _date(post.get('date')), _date(post.get('observed_at'))
        if published is None or observed is None:
            continue
        hours = (observed - published).total_seconds() / 3600
        if not lower <= hours <= upper:
            continue
        views, subs = post.get('views'), post.get('subs_at_observation')
        known = type(views) is int and views >= 0 and type(subs) is int and subs > 0
        candidates.append({'id': post['id'], 'url': 'https://t.me/' + post['id'], 'views': views,
                           'subscribers': subs, 'age_hours': hours, 'observed_at': post['observed_at'],
                           'ratio': views / subs * 100 if known else None})
    selected = min(candidates, key=lambda post: (abs(post['age_hours'] - target), -post['age_hours'], post['id'])) if candidates else None
    passed = False
    if selected is None:
        uncertain.append(f'Нет замера поста возрастом {lower}–{upper} ч для оценки активности')
    elif selected['ratio'] is None:
        uncertain.append('У ближайшего к заданному возрасту поста не распознаны просмотры или подписчики')
    else:
        passed = selected['views'] * 100 >= selected['subscribers'] * settings.min_post_view_ratio
        if not passed:
            failures.append(f"Посту {selected['age_hours']:.1f} ч: просмотры/подписчики ниже {settings.min_post_view_ratio:g}%")
    row.update(norm=float(settings.min_post_view_ratio), activity_status='pass' if passed else 'below' if failures else 'unknown',
               activity_ratio=round(selected['ratio'], 4) if selected and selected['ratio'] is not None else None,
               activity_post_url=selected['url'] if selected else '',
               activity_post_age_hours=round(selected['age_hours'], 2) if selected else None,
               activity_views=selected['views'] if selected else None,
               activity_subscribers=selected['subscribers'] if selected else None,
               activity_observed_at=selected['observed_at'] if selected else '',
               activity_target_age_hours=target, activity_age_tolerance_hours=tolerance,
               activity_evidence=selected, metric_rule_version=METRIC_VERSION)
    return failures, uncertain
