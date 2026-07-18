import re
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

import dkim
from slugify import slugify
from feedgen.feed import FeedGenerator
from pathvalidate import sanitize_filename

from config import (
  log,
  ROOT,
  BASE_URL,
  SITE_TITLE,
  ALLOWED_SENDER,
  SUBJ_PREFIX,
  DEFAULT_PUBLISH_PATH,
  FEED_MAX_ENTRIES,
)

# -- Mail-related

def check_dkim(mail, mail_obj):
  # Require a valid signature whose signing domain is ALLOWED_SENDER's domain.
  sender_domain = ALLOWED_SENDER.rpartition('@')[2].lower()
  sig = dkim.DKIM(mail)

  for idx in range(len(mail_obj.get_all('DKIM-Signature', []))):
    try:
      if not sig.verify(idx):
        continue
    except dkim.DKIMException:
      # We'll eventually end loop and return False anyway
      continue
    if sig.domain.decode().lower().rstrip('.') == sender_domain:
      return True

  return False


def verify(mail, mail_obj):

  if not check_dkim(mail, mail_obj):
    log.warning('No proper DKIM signature for %s', mail_obj.get('Subject'))
    return False

  if parseaddr(mail_obj.get('From') or '')[1] != ALLOWED_SENDER:
    return False

  if not mail_obj.get('Subject').startswith(SUBJ_PREFIX) :
    log.info('Ignoring message with wrong subject: %r', mail_obj.get('Subject'))
    return False

  body = text_part(mail_obj)
  if body is None:
    log.warning('no text/plain part: %r', mail_obj.get('Subject'))
    return False

  return True


def msg_date(mail):
  dt = parsedate_to_datetime(mail.get('Date')) if mail.get('Date') else None
  if dt is None:
    return datetime.now(timezone.utc)
  return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def text_part(mail):
  body = None

  part = mail.get_body(preferencelist=('plain',))
  if part: body = part.get_content()

  for part in mail.walk():
    if part.get_content_type() == 'text/plain' \
     and part.get_content_disposition() != 'attachment':
      body = part.get_content()

  # otherwise replace will blow up
  if body is None:
    return None

  # Normalize line endings while at it
  return body.replace('\r\n', '\n').replace('\r', '\n')


def split_publish_path(text):
  # Falls back to preconfigured path if there is none

  lines = text.split('\n')
  first = lines[0].strip() if lines else ''

  if first.startswith('/'):
    return first, '\n'.join(lines[1:]).lstrip('\n')

  return DEFAULT_PUBLISH_PATH, text


def get_abs_path(suffix=''):
  rel = suffix.strip().lstrip('/')
  path = ROOT / rel
  if not path.is_dir():
    raise FileNotFoundError(f'Publish directory does not exist: {path}')
  return path


def extract_attachments(mail):
  out = []
  for part in mail.iter_attachments():
    # Just in case we sent message containing html part
    if part.get_content_type().lower() == 'text/html': continue
    out.append((
      sanitize_filename(part.get_filename()),
      part.get_payload(decode=True)
    ))

  return out

# -- Processing text

def reflow(text):
  # I need to unwrap email that usually gets wrapped at 72 charcters

  verbatim = False
  flush_buf = ''
  # -- is used for email signatures, I need an explicit line break on it
  tag_re = re.compile(r'^(#{1,3}|\*|>|=>|--)\s*')
  result = list()

  for line in text.splitlines():
    line = line.rstrip()

    # If the tag was ```, toggle verbatim output
    if line.startswith('```'):
      verbatim = not verbatim

    if verbatim:
      result.append(line)
      continue

    # Line is a tag, flush buffer if any and set line to buffer
    if (groups := tag_re.match(line)):
      if flush_buf:
        result.append(flush_buf)
        flush_buf = ''
      flush_buf = line
    # Line is normal and buffer non-empty, append to buffer
    elif line and flush_buf:
      flush_buf = flush_buf + ' ' + line
    # Line is normal; buffer empty, set buffer
    elif line:
      flush_buf = line
    # Line empty, and buffer set, flush buffer
    elif flush_buf:
      result.append(flush_buf)
      result.append('')
      flush_buf = ''
    # Line empty and buffer empty, flush line
    else:
      result.append(line)

  # Possible if there was last line in email with no signature and no empty lines after it
  if flush_buf:
    result.append(flush_buf)

  output = '\n'.join(result)
  return output


def replace_placeholders(text, filenames):
  fn_iter = iter(filenames)

  def replace_cb(reg_obj):
    try:
      fname = next(fn_iter)
      return f'=> {fname}'

    except StopIteration:
      log.warning(f'Unable to replace %s, no more attachments', reg_obj)
      return reg_obj.group(0)

  # Only touch '=> {}', leave the rest of line
  return re.sub(r'=> {}', replace_cb, text)


def save_article(article, title, article_path):
  with open(article_path, 'w') as handle:
    handle.write(f'# {title}\n\n')
    handle.write(article)

# -- Writing index and atom

def insert_index_entry(index, article, title):
  new_entry = f"=> {article} {title}\n"

  with open(index, 'r+') as handle:
    lines = handle.readlines()

    # Look for first line starting with date link
    insert_idx = len(lines)
    for i, line in enumerate(lines):
      if re.match(r'^=> \d{4}-\d{2}-\d{2}', line):
        insert_idx = i
        break

    lines.insert(insert_idx, new_entry)

    # Overwrite
    handle.seek(0)
    handle.writelines(lines)
    handle.truncate()


def update_atom(title, path, date):
  # Prepare new article object
  item = {'title': title, 'path': path, 'updated': date.isoformat()}

  # Update cache
  cache_path = get_abs_path() / '.atom-feed-cache.json'
  with open(cache_path, 'r') as handle:
    entries = json.load(handle)

  entries.insert(0, item)
  entries = entries[:FEED_MAX_ENTRIES]

  # Make feed
  fg = FeedGenerator()
  fg.id(BASE_URL)
  fg.title(SITE_TITLE)
  fg.updated(date.isoformat())
  fg.link(href=BASE_URL, rel='alternate')
  fg.link(href=BASE_URL + 'atom.xml', rel='self')

  for item in entries:
    fe = fg.add_entry()
    url = BASE_URL + item['path']
    fe.id(url)
    fe.title(item['title'])
    fe.link(href=url)
    fe.updated(date.isoformat())
  fg.atom_file(str(ROOT / 'atom.xml'), pretty=True)

  # Store cache if nothing blew up
  with open(cache_path, 'w') as handle:
    json.dump(entries, handle, indent=2)


def publish(mail):

  mail_obj = BytesParser(policy=policy.default).parsebytes(mail)
  if not verify(mail, mail_obj):
    log.info(f'Skipping {mail_obj["Subject"]}')
    return True

  # Prepare metadata and raw body
  raw_body = text_part(mail_obj)
  title = mail_obj['Subject'].strip().replace(SUBJ_PREFIX, '')
  date = parsedate_to_datetime(mail_obj['Date'])  # Fails if there is no date
  log.info(f'Publishing %s', title)

  path_suffix, raw_body = split_publish_path(raw_body)
  out_dir = get_abs_path(path_suffix)

  article_prefix = f'{date:%Y-%m-%d}-{slugify(title, max_length=20, word_boundary=True)}'
  article_attach_dir = out_dir / article_prefix
  article_file = out_dir / f'{article_prefix}.gmi'

  # Store attachments first so we get a list of tokens for replacements
  # FIXME: This'll leave dangling files if article processing fails
  attachments = []
  for name, data in extract_attachments(mail_obj):
    if not article_attach_dir.exists(): article_attach_dir.mkdir()
    with open(article_attach_dir / name, 'wb') as handle:
      handle.write(data)
    attachments.append(f'{article_prefix}/{name}')

  # Process the article itself
  article = reflow(raw_body)
  article = replace_placeholders(article, attachments)
  save_article(article, title, article_file)

  # Add entry to the relevant index page
  title_date = f'{date:%Y-%m-%d} {title}'
  insert_index_entry(
    out_dir / 'index.gmi',
    f'{article_prefix}.gmi',
    title_date)

  # Update feed
  update_atom(
    title,
    f'{path_suffix}/{article_prefix}.gmi',
    date)

  return True
