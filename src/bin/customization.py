"""Validated personalization settings and model-independent text formatting."""
import copy
import fcntl
import json
import os
import re
import tempfile

DEFAULTS = {
    'bar_theme': 'dark', 'settings_theme': 'dark', 'bar_size': 'compact', 'bar_opacity': 97,
    'developer_logging': False, 'show_model': True, 'punctuation': 'model', 'capitalization': 'model',
    'spoken_punctuation': False, 'trailing_space': True, 'vocabulary': [],
}
THEMES = ('dark', 'light', 'contrast', 'ocean', 'forest', 'plum', 'midnight', 'warm')
CHOICES = {'bar_theme': THEMES, 'settings_theme': THEMES,
           'bar_size': ('tiny', 'compact', 'comfortable', 'large'),
           'punctuation': ('model', 'none'),
           'capitalization': ('model', 'sentence', 'lower')}


def validate(values):
    out = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        if key in CHOICES:
            if value not in CHOICES[key]:
                raise ValueError('Choose a valid value for ' + key.replace('_', ' ') + '.')
        elif key == 'bar_opacity':
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError('Bar opacity must be between 0 and 100 percent.')
        elif key == 'vocabulary':
            if not isinstance(value, list) or len(value) > 200:
                raise ValueError('Use up to 200 vocabulary rules.')
            seen = set()
            clean = []
            for rule in value:
                if not isinstance(rule, dict):
                    raise ValueError('Each vocabulary rule needs a heard phrase and preferred text.')
                a, b = rule.get('heard'), rule.get('write')
                if not all(isinstance(t, str) and t.strip() and len(t) <= 200 and not any(ord(c) < 32 for c in t) for t in (a, b)):
                    raise ValueError('Vocabulary phrases must contain 1–200 characters, without line breaks.')
                a, b = a.strip(), b.strip()
                if a.casefold() in seen:
                    raise ValueError('Duplicate heard phrase: ' + a)
                seen.add(a.casefold())
                clean.append({'heard': a, 'write': b})
            value = clean
        elif type(value) is not bool:
            raise ValueError(key.replace('_', ' ').capitalize() + ' must be on or off.')
        out[key] = value
    return out


def settings(config):
    out = copy.deepcopy(DEFAULTS)
    # A manually edited invalid field must not prevent the app from starting.
    for key in DEFAULTS:
        if key in config:
            try: out.update(validate({key: config[key]}))
            except ValueError: pass
    return out


def update_config(path, changes):
    """Merge atomically under a lock so concurrent settings writes keep other fields."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f: config = json.load(f)
        except FileNotFoundError: config = {}
        if not isinstance(config, dict):
            raise ValueError('Settings file is not a JSON object. Restore a valid config.json before saving.')
        config.update(changes)
        fd, tmp = tempfile.mkstemp(prefix='.config-', dir=parent)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(config, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    return config


SPOKEN = {'exclamation mark': '!', 'exclamation point': '!', 'question mark': '?',
          'full stop': '.', 'period': '.', 'comma': ',', 'semicolon': ';', 'colon': ':'}


def format_text(text, config):
    s = settings(config)
    t = text.strip()
    t = re.sub(r'[,.]?\s*\bnew paragraph\b[.,]?\s*', '\n\n', t, flags=re.I)
    t = re.sub(r'[,.]?\s*\bnew line\b[.,]?\s*', '\n', t, flags=re.I)
    if s['punctuation'] == 'none':
        t = re.sub(r'[.,!?;:]+', '', t)
    if s['spoken_punctuation']:
        # Consume model punctuation around the command to avoid doubled marks.
        pattern = r'[.,!?;:]*[ \t]*\b(' + '|'.join(SPOKEN) + r')\b[.,!?;:]*'
        t = re.sub(pattern, lambda m: SPOKEN[m[1].lower()], t, flags=re.I)
    if s['capitalization'] == 'lower':
        t = t.lower()
    elif s['capitalization'] == 'sentence':
        t = re.sub(r'(^|[.!?]\s+|\n+)([^\W\d_])', lambda m: m[1] + m[2].upper(), t)
    rules = s['vocabulary']
    if rules:
        # One pass, longest phrases first; never replace inside another word or cascade rules.
        ordered = sorted(rules, key=lambda r: len(r['heard']), reverse=True)
        pattern = r'(?<!\w)(?:' + '|'.join('(' + re.escape(r['heard']) + ')' for r in ordered) + r')(?!\w)'
        t = re.sub(pattern, lambda m: ordered[m.lastindex - 1]['write'], t, flags=re.I)
    return t


def whole_phrase_required(config):
    s = settings(config)
    return bool(s['vocabulary'] or s['spoken_punctuation'] or s['punctuation'] != 'model'
                or s['capitalization'] != 'model' or not s['trailing_space'])


def final_text(text, config):
    out = format_text(text, config)
    return out + (' ' if out and not out.endswith('\n') and settings(config)['trailing_space'] else '')
