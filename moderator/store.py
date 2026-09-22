import hashlib
import json
import sqlite3
import time

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS members(
            chat INTEGER, user INTEGER, epoch INTEGER, active INTEGER, stamp INTEGER,
            PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS messages(
            chat INTEGER,user INTEGER,epoch INTEGER,message INTEGER,
            PRIMARY KEY(chat,user,epoch,message));
        CREATE TABLE IF NOT EXISTS jobs(
            key TEXT PRIMARY KEY,kind TEXT,payload TEXT,revision INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,due REAL DEFAULT 0,
            error TEXT,created REAL,fingerprint TEXT);
        CREATE TABLE IF NOT EXISTS audit(
            at REAL,chat INTEGER,user INTEGER,action TEXT,detail TEXT);
        ''')
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
        self.db.commit()

    def close(self): self.db.close()

    def member(self, chat, user):
        return self.db.execute('SELECT * FROM members WHERE chat=? AND user=?', (chat,user)).fetchone()

    def join(self, chat, user, stamp):
        old = self.member(chat,user)
        if old and (stamp < old['stamp'] or old['active']):
            return None
        epoch = old['epoch'] + 1 if old else 1
        self.db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,1,?)', (chat,user,epoch,stamp))
        self.db.commit()
        return epoch

    def leave(self, chat, user, stamp):
        self.db.execute('UPDATE members SET active=0,stamp=? WHERE chat=? AND user=? AND stamp<=?',
                        (stamp,chat,user,stamp))
        self.db.commit()

    def first_three(self, chat, user, mid, edited, unseen=False):
        member = self.member(chat,user)
        if member is None and unseen and not edited:
            self.join(chat,user,0)
            member = self.member(chat,user)
        if not member or not member['active']: return None
        epoch = member['epoch']
        args = (chat,user,epoch)
        if self.db.execute('SELECT 1 FROM messages WHERE chat=? AND user=? AND epoch=? AND message=?',
                           (*args,mid)).fetchone(): return epoch
        if edited: return None
        count = self.db.execute('SELECT count(*) FROM messages WHERE chat=? AND user=? AND epoch=?',args).fetchone()[0]
        if count >= 3: return None
        self.db.execute('INSERT INTO messages VALUES(?,?,?,?)',(*args,mid))
        self.db.commit()
        return epoch

    def put(self, key, kind, payload, replace=False):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        old = self.db.execute('SELECT fingerprint FROM jobs WHERE key=?',(key,)).fetchone()
        if old:
            if not replace or old['fingerprint'] == fingerprint: return
            self.db.execute("UPDATE jobs SET payload=?,fingerprint=?,revision=revision+1,status='pending',attempts=0,due=0,error=NULL WHERE key=?",
                            (body,fingerprint,key))
        else:
            self.db.execute('INSERT INTO jobs(key,kind,payload,created,fingerprint) VALUES(?,?,?,?,?)',
                            (key,kind,body,time.time(),fingerprint))
        self.db.commit()

    def claim(self, actions=None):
        category = "" if actions is None else (" AND kind IN ('delete','kick')" if actions else " AND kind NOT IN ('delete','kick')")
        row = self.db.execute("""SELECT * FROM jobs WHERE status='pending' AND due<=?""" + category + """
          ORDER BY CASE WHEN kind IN ('delete','kick') THEN 0 ELSE 1 END,created LIMIT 1""",(time.time(),)).fetchone()
        if row is None: return None
        self.db.execute("UPDATE jobs SET status='running' WHERE key=?",(row['key'],))
        self.db.commit()
        result = dict(row)
        result['payload'] = json.loads(row['payload'])
        return result

    def current(self, job):
        row = self.db.execute('SELECT revision FROM jobs WHERE key=?',(job['key'],)).fetchone()
        return bool(row and row['revision'] == job['revision'])

    def finish(self, job):
        self.db.execute("UPDATE jobs SET status='done',payload='{}' WHERE key=? AND revision=?",(job['key'],job['revision']))
        self.db.commit()

    def checkpoint(self, job):
        self.db.execute('UPDATE jobs SET payload=? WHERE key=? AND revision=?',
            (json.dumps(job['payload'],ensure_ascii=False),job['key'],job['revision']))
        self.db.commit()

    def defer(self, job, delay):
        # Waiting for group initialization does not consume operation retries.
        self.db.execute("UPDATE jobs SET status='pending',due=? WHERE key=? AND revision=?",
                        (time.time()+delay,job['key'],job['revision']))
        self.db.commit()

    def fail(self, job, error, maximum, delay):
        attempts = job['attempts'] + 1
        status = 'failed' if attempts >= maximum else 'pending'
        self.db.execute('UPDATE jobs SET status=?,attempts=?,due=?,error=? WHERE key=? AND revision=?',
            (status,attempts,time.time()+delay,error,job['key'],job['revision']))
        self.db.commit()
        return status

    def audit(self, chat, user, action, detail):
        self.db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(time.time(),chat,user,action,detail))
        self.db.commit()

    def retry_failed(self):
        cursor = self.db.execute("UPDATE jobs SET status='pending',attempts=0,due=0 WHERE status='failed'")
        self.db.commit()
        return cursor.rowcount
