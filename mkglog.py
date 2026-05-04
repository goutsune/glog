#!/usr/bin/env python3
import json
import mailbox
import os
import re
import tempfile
import unicodedata

from pathlib import Path
from datetime import datetime, timezone
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from feedgen.feed import FeedGenerator

from config import log

placeholder_re = re.compile(r'^(=> )\{\}(\s+.*)?$', re.MULTILINE)
LIST_RE = re.compile(r'^\d+\.\s')
LINK_RE = re.compile(r'^=> ')


def public_path(suffix, name):
  return f'{rel}/{name}' if rel else f'/{name}'


def atomic_write(path, data):
  os.makedirs(path.parent, exist_ok=True)
  with open(path, 'wb') as f:
    f.write(data)


def msg_date(msg):
  try:
    dt = parsedate_to_datetime(msg.get('Date')) if msg.get('Date') else None
    if dt is None:
      raise ValueError
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
  except Exception:
    return datetime.now(timezone.utc)


def attachments(msg):
  out = []
  for i, part in enumerate(msg.iter_attachments(), 1):
    if part.get_content_type().lower() == 'text/html':
      continue
    name = Path(part.get_filename() or f'attachment-{i}.bin').name
    stem = unicodedata.normalize('NFKD', Path(name).stem).encode('ascii', 'ignore').decode()
    stem = re.sub(r'[^a-zA-Z0-9._-]+', '-', stem).strip('.-_') or f'attachment-{i}'
    suffix = re.sub(r'[^a-zA-Z0-9.]', '', Path(name).suffix.lower()) or '.bin'
    out.append((stem + suffix, part.get_payload(decode=True) or b''))
  return out

def reflow(text: str) -> str:
  out: list[str] = []
  para: list[str] = []
  empty_count = 0
  verbatim = False

  def flush_para() -> None:
    nonlocal para
    if para:
      out.append(' '.join(s.strip() for s in para))
      para = []

  def flush_empty() -> None:
    nonlocal empty_count
    if empty_count:
      out.extend('' for _ in range(empty_count))
      empty_count = 0

  for raw in text.splitlines():
    line = raw.rstrip()
    stripped = line.strip()

    # Gemini preformatted block fence.
    # Fence line and all contents are preserved verbatim until next fence.
    if stripped.startswith('```'):
      flush_para()
      flush_empty()
      out.append(line)
      verbatim = not verbatim
      continue

    if verbatim:
      out.append(line)
      continue

    # Empty line: ends paragraph, preserve runs of empty lines.
    if not stripped:
      flush_para()
      empty_count += 1
      continue

    special = (
      stripped.startswith('=>')
      or stripped.startswith('#')
      or stripped.startswith('*')
      or stripped.startswith('--')  # Mail signature
      or LIST_RE.match(stripped) is not None
    )

    if special:
      flush_para()
      flush_empty()
      out.append(line)
      continue

    # Normal text line: merge with adjacent normal text until empty/special.
    flush_empty()
    para.append(stripped)

  flush_para()
  flush_empty()

  return '\n'.join(out).rstrip() + '\n'


def replace_placeholders(body, files, article_dir):
  hits = len(placeholder_re.findall(body))
  if len(files) > hits:
    log.warning('%d extra attachment(s) without placeholder', len(files) - hits)
  if hits > len(files):
    log.warning('%d placeholder(s) without attachment', hits - len(files))
  i = 0

  def sub(m: re.Match[str]) -> str:
    nonlocal i
    if i >= len(files):
      return m.group(0)
    rel = './' + files[i].relative_to(article_dir).as_posix()
    i += 1
    return f'{m.group(1)}{rel}{m.group(2) or ''}'

  return placeholder_re.sub(sub, body)


def insert_index_entry(index, filename, title):
  lines = index.read_text(encoding='utf-8').splitlines()
  entry = f'=> {filename} {title}'
  pos = next((i for i, line in enumerate(lines) if LINK_RE.match(line)), len(lines))
  lines.insert(pos, entry)
  atomic_write(index, ('\n'.join(lines) + '\n').encode())


def load_feed_cache(path):
  try:
    data = json.loads(path.read_text(encoding='utf-8'))
    return data if isinstance(data, list) else []
  except FileNotFoundError:
    return []


def save_feed_cache(path, entries):
  atomic_write(path, (json.dumps(entries[:FEED_MAX_ENTRIES], indent=2) + '\n').encode())


def make_atom(feed_dir):
  entries = load_feed_cache(feed_dir / FEED_CACHE_NAME)[:FEED_MAX_ENTRIES]
  fg = FeedGenerator()
  fg.id(BASE_URL + '/')
  fg.title(SITE_TITLE)
  fg.link(href=BASE_URL + '/', rel='alternate')
  fg.link(href=BASE_URL + '/' + FEED_NAME, rel='self')
  fg.updated(datetime.fromisoformat(entries[0]['updated']) if entries else datetime.now(timezone.utc))
  for item in entries:
    fe = fg.add_entry()
    url = BASE_URL + item['path']
    fe.id(url)
    fe.title(item['title'])
    fe.link(href=url)
    fe.updated(datetime.fromisoformat(item['updated']))
  fg.atom_file(str(ROOT / FEED_NAME), pretty=True)


def mark_seen(box: mailbox.Maildir, key: str) -> None:
  msg = box.get_message(key)
  msg.set_subdir('cur')
  msg.add_flag('S')
  box[key] = msg
  box.flush()


def main():
  os.umask(0o022)  # Molly refuses to serve non-world-readable files

  feed_dir = publish_dir(FEED_PUBLISH_PATH)
  count = 0
  box = mailbox.Maildir(str(MAILDIR), create=False)

  try:
    for key in list(box.keys()):
      if box.get_message(key).get_subdir() != 'new':
        continue
      try:
        if publish(box.get_bytes(key)):
          mark_seen(box, key)
          count += 1
      except Exception:
        log.exception('failed to process Maildir message %s', key)
  finally:
    box.close()

  make_atom(feed_dir)

  log.info('Published %d message(s)', count)
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
