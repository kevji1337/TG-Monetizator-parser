"""Literal excerpts and selectable facts. No model or network needed."""
import re

PRICE = re.compile(r'(?<!\w)\d[\d \u00a0.,]{0,15}\s*(?:₽|руб\.?|BYN|USD|EUR|\$|€)(?!\w)', re.I)
CONTACT = re.compile(r'(?<!\w)@[A-Za-z][A-Za-z0-9_]{3,31}\b|\bв\s+личн\w*\s+сообщени\w*|\bв\s+директ\b', re.I)


def fragments(text, maximum=700):
    """Cover a source in consecutive exact excerpts, split near word boundaries."""
    position = 0
    while position < len(text):
        end = min(position + maximum, len(text))
        if end < len(text):
            breaks = list(re.finditer(r'\s+', text[position + maximum // 2:end]))
            if breaks:
                end = position + maximum // 2 + breaks[-1].end()
        quote = text[position:end].strip()
        if quote:
            yield quote
        position = end


def catalogue(data):
    """Stable reference IDs address the exact same snapshot used in validation."""
    sources, excerpts, facts = [], {}, {}
    for index, source in enumerate(data['sources'], 1):
        prefix = f's{index:02d}'
        entries = []
        for number, quote in enumerate(fragments(source['text']), 1):
            ref = f'{prefix}q{number:03d}'
            excerpts[ref] = {'source_id': source['id'], 'quote': quote}
            entries.append({'ref': ref, 'text': quote})
        price_options, booking_options = [], []
        for kind, pattern, options, letter in [('price', PRICE, price_options, 'p'),
                                                ('booking', CONTACT, booking_options, 'b')]:
            seen = set()
            for entry in entries:
                for match in pattern.finditer(entry['text']):
                    value = match.group().strip()
                    if value in seen:
                        continue
                    seen.add(value)
                    ref = f'{prefix}{letter}{len(options) + 1:03d}'
                    fact = {'source_id': source['id'], 'kind': kind, 'value': value,
                            'quote_ref': entry['ref'], 'origin': 'text'}
                    facts[ref] = fact
                    options.append({'ref': ref, 'value': value, 'quote_ref': entry['ref']})
        for number, link in enumerate(dict.fromkeys(source.get('links', [])), 1):
            if not isinstance(link, str) or not link:
                continue
            ref = f'{prefix}l{number:03d}'
            quote_ref = next((entry['ref'] for entry in entries if link in entry['text']), '')
            facts[ref] = {'source_id': source['id'], 'kind': 'booking', 'value': link,
                          'quote_ref': quote_ref, 'origin': 'html_link'}
            booking_options.append({'ref': ref, 'value': link, 'quote_ref': quote_ref})
        sources.append({**{key: source[key] for key in ('id', 'url', 'date', 'forwarded', 'truncated') if key in source},
                        'fragments': entries, 'price_options': price_options, 'booking_options': booking_options})
    return sources, excerpts, facts


def prompt_data(data):
    sources, _, _ = catalogue(data)
    return {**data, 'sources': sources}
