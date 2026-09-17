import re

with open('virtual_broker.py', 'r') as f:
    content = f.read()

# Add queue and threading imports
if 'import queue' not in content:
    content = content.replace('import datetime', 'import datetime\nimport threading\nimport queue\nimport time')

# Add initialization of DB thread in __init__
init_replacement = """        self.initial_deposit = config.get('initial_deposit_per_strategy', 100.0)

        # Async DB Queue setup
        self.db_queue = queue.Queue()
        self.db_thread = threading.Thread(target=self._db_worker, daemon=True)
        self.db_thread.start()

        self._init_db()"""

content = content.replace("        self.initial_deposit = config.get('initial_deposit_per_strategy', 100.0)\n\n        self._init_db()", init_replacement)

# Add the db worker thread function
worker_func = """    def _db_worker(self):
        while True:
            try:
                task = self.db_queue.get()
                if task is None:
                    break
                query, params = task
                with sqlite3.connect(self.db_path, timeout=10) as conn:
                    cur = conn.cursor()
                    cur.execute(query, params)
                    conn.commit()
            except sqlite3.Error as e:
                print(f"DB worker error: {e}")
            except Exception as e:
                print(f"DB worker error (general): {e}")
            finally:
                self.db_queue.task_done()

    def _init_db(self):"""

content = content.replace("    def _init_db(self):", worker_func)


# Replace synchronous sqlite3 connections in save_equity_snapshot and _record_trade_in_db
record_trade_old = """    def _record_trade_in_db(self, strategy: str, direction: str, size: float,
                            entry_time: str, entry_price: float, exit_time: str, exit_price: float,
                            exit_reason: str, pnl_gross: float, fees_paid: float, pnl_net: float, notes: str):
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO trades
                (strategy, direction, size, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_gross, fees_paid, pnl_net, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (strategy, direction, size, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_gross, fees_paid, pnl_net, notes))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[{strategy}] DB Error saving trade: {e}")"""

record_trade_new = """    def _record_trade_in_db(self, strategy: str, direction: str, size: float,
                            entry_time: str, entry_price: float, exit_time: str, exit_price: float,
                            exit_reason: str, pnl_gross: float, fees_paid: float, pnl_net: float, notes: str):
        try:
            query = '''
                INSERT INTO trades
                (strategy, direction, size, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_gross, fees_paid, pnl_net, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            params = (strategy, direction, size, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_gross, fees_paid, pnl_net, notes)
            self.db_queue.put((query, params))
        except Exception as e:
            print(f"[{strategy}] Error queueing trade save: {e}")"""

content = content.replace(record_trade_old, record_trade_new)

save_equity_old = """    def save_equity_snapshot(self, timestamp_iso: str):
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cur = conn.cursor()
            for strategy in ['DLH', 'HRB', 'ARGUS', 'JAZZ']:
                w, u, t = self.get_account_equity(strategy)
                cur.execute('''
                    INSERT INTO equity_snapshots (timestamp, strategy, wallet_balance, unrealized_pnl, total_equity)
                    VALUES (?, ?, ?, ?, ?)
                ''', (timestamp_iso, strategy, w, u, t))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"DB Error saving equity snapshot: {e}")"""

save_equity_new = """    def save_equity_snapshot(self, timestamp_iso: str):
        try:
            for strategy in ['DLH', 'HRB', 'ARGUS', 'JAZZ']:
                w, u, t = self.get_account_equity(strategy)
                query = '''
                    INSERT INTO equity_snapshots (timestamp, strategy, wallet_balance, unrealized_pnl, total_equity)
                    VALUES (?, ?, ?, ?, ?)
                '''
                params = (timestamp_iso, strategy, w, u, t)
                self.db_queue.put((query, params))
        except Exception as e:
            print(f"Error queueing equity snapshot: {e}")"""

content = content.replace(save_equity_old, save_equity_new)

with open('virtual_broker.py', 'w') as f:
    f.write(content)
