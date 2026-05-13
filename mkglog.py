#!/usr/bin/env python3
import mailbox
import os

from config import log, MAILDIR
from publisher import publish

def mark_seen(box, key):
  msg = box.get_message(key)
  msg.set_subdir('cur')
  msg.add_flag('S')
  box[key] = msg
  box.flush()


def main():
  os.umask(0o022)  # Molly refuses to serve non-world-readable files

  count = 0
  box = mailbox.Maildir(str(MAILDIR), create=False)

  try:
    for key in list(box.keys()):
      if box.get_message(key).get_subdir() != 'new':
        continue

      if publish(box.get_bytes(key)):
        mark_seen(box, key)
        count += 1
  finally:
    box.close()

  log.info('Published %d message(s)', count)
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
