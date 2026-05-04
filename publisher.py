import dkim
from types import SimpleNamespace
from email.parser import BytesParser
from email.utils import parseaddr
from slugify import slugify

from config import (
  ALLOWED_SENDER,
  SUBJ_PREFIX,
  FEED_PUBLISH_PATH,
  FEED_CACHE_NAME,
  DEFAULT_PUBLISH_PATH,
)


def verify(mail):
  if not dkim.verify(mail):
    log.warning('invalid DKIM')
    return False

  mail_obj = BytesParser(policy=policy.default).parsebytes(mail)

  if parseaddr(mail_obj.get('From'))[1] != ALLOWED_SENDER:
    return False

  if not mail_obj.get('Subject').startswith(SUBJ_PREFIX) :
    log.info('Ignoring message with wrong subject: %r', mail_obj.get('Subject'))
    return False

  body = text_part(mail_obj)
  if body is None:
    log.warning('no text/plain part: %r', mail_obj.get('Subject'))
    return False

  return True


def text_part(mail):
  body = None

  part = mail.get_body(preferencelist=('plain',))
  if part: body = part.get_content()

  for part in mail.walk():
    if part.get_content_type() == 'text/plain' \
     and part.get_content_disposition() != 'attachment':
      body = part.get_content()

  # Normalize line endings while at it
  body = body.replace('\r\n', '\n').replace('\r', '\n')

  return body


def split_publish_path(text):
  # Falls back to preconfigured path if there is none

  lines = text.split('\n')
  first = lines[0].strip() if lines else ''

  if first.startswith('/'):
    return first, '\n'.join(lines[1:]).lstrip('\n')

  return DEFAULT_PUBLISH_PATH, text


def publish_dir(suffix):
  rel = suffix.strip().lstrip('/')
  path = ROOT / rel
  if not path.is_dir():
    raise FileNotFoundError(f'publish directory does not exist: {path}')
  return path


def publish(mail):

  mail_obj = BytesParser(policy=policy.default).parsebytes(mail)
  if not verify(mail): return None

  body = text_part(mail_obj)
  title = mail_obj.get('Subject').strip().replace(SUBJ_PREFIX, '')
  date = msg_date(mail_obj)

  suffix, body = split_publish_path(body)
  out_dir = publish_dir(suffix)

  article_fn = f'{date:%Y-%m-%d}-{slugify(title, 20)}'
  article_path = out_dir / f'{article_fn}.gmi'
  article_attach_dir = out_dir / article_fn

  # That's it for today, need to rewrite more of this sludge later
  saved = []
  for name, data in attachments(mail_obj):
    article_attach_dir.mkdir(exist_ok=False) if not article_attach_dir.exists() else None
    saved.append(save_unique(article_attach_dir / name, data))

  body = replace_placeholders(reflow(body), saved, out_dir)
  atomic_write(article, f'# {title}\n\n{body.strip()}\n'.encode())
  title_date = f'{date:%Y-%m-%d} {title}'
  insert_index_entry(out_dir / 'index.gmi', article_path, title_date)

  item = {
    'title': title,
    'path': f'{suffix}/{article_path}' if suffix else f'/{article_path}',
    'updated': date.isoformat()
  }
  cache_path = publish_dir(FEED_PUBLISH_PATH) / FEED_CACHE_NAME
  save_feed_cache(
    cache_path,
    [item] + [
      x for x in load_feed_cache(cache_path)
        if x.get('path') != item['path']
    ]
  )
  return item
