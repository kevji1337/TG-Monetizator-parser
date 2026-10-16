"""Exact TGStat form values; independent of public-preview audit thresholds."""


def filter_spec(settings, category_id=None):
    def text(value):
        return '' if value is None else str(value)
    return {
        'text': {'q': '' if category_id is not None else None,
                 'participantscountfrom': text(settings.min_subs), 'participantscountto': text(settings.max_subs),
                 'avgreachfrom': text(settings.tgstat_min_avg_reach), 'avgreachto': text(settings.tgstat_max_avg_reach),
                 'avgreach24from': text(settings.min_daily_reach), 'avgreach24to': text(settings.max_daily_reach),
                 'cifrom': text(settings.min_ci), 'cito': text(settings.max_ci)},
        'selects': {'categories': [category_id] if category_id else [], 'countries': list(settings.country_ids),
                    'languages': list(settings.language_ids), 'channeltype': [settings.channel_type]},
        'sliders': {'age': {'from': settings.min_age_months, 'to': settings.max_age_months if settings.max_age_months is not None else 120},
                    'err': {'from': settings.min_err, 'to': settings.max_err if settings.max_err is not None else 100},
                    'er': {'from': settings.min_er}, 'male': {'from': settings.min_male}, 'female': {'from': settings.min_female}},
        'checks': {'inabout': False, 'nodead': settings.exclude_dead, 'noscam': settings.exclude_scam,
                   'noredlabel': settings.exclude_red_label, 'isverified': False, 'isrknverified': False, 'isstoriesavailable': False},
    }
