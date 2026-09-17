import os
import json
import sqlite3
import datetime
import threading
import queue
from typing import Optional, Dict, Any, List, Union

class VirtualBroker:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.symbol = config.get('symbol', 'ETHUSDT')
        self.leverage = config.get('leverage', 15)
        self.maker_fee = config.get('maker_fee', 0.0002)
        self.taker_fee = config.get('taker_fee', 0.0005)
        self.limit_penetration = config.get('limit_fill_penetration_usdt', 0.10)
        self.db_path = config.get('db_path', 'simulation.db')
        self.state_path = config.get('state_path', 'simulation_state.json')
        self.initial_deposit = config.get('initial_deposit_per_strategy', 100.0)

        self._init_db()
        self._db_queue: queue.Queue = queue.Queue()
        self._stop_db_event = threading.Event()
        self._db_thread = threading.Thread(target=self._db_worker, daemon=True)
        self._db_thread.start()
        self.state = self._load_or_init_state()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, direction TEXT NOT NULL, size REAL NOT NULL, entry_time TEXT NOT NULL, entry_price REAL NOT NULL, exit_time TEXT, exit_price REAL, exit_reason TEXT, pnl_gross REAL, fees_paid REAL, pnl_net REAL, notes TEXT)')
        cur.execute('CREATE TABLE IF NOT EXISTS equity_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, strategy TEXT NOT NULL, wallet_balance REAL NOT NULL, unrealized_pnl REAL NOT NULL, total_equity REAL NOT NULL)')
        conn.commit()
        conn.close()

    def _db_worker(self):
        """Фоновый рабочий поток для асинхронной записи в SQLite без блокировки тикового цикла."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        cur = conn.cursor()
        while not self._stop_db_event.is_set():
            try:
                task = self._db_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break
            query, params = task
            try:
                cur.execute(query, params)
                conn.commit()
            except Exception as e:
                print(f"[VirtualBroker DB Error] {e}")
            finally:
                self._db_queue.task_done()
        conn.close()

    def _enqueue_db_write(self, query: str, params: tuple):
        """Добавление SQL-запроса в потокобезопасную очередь."""
        self._db_queue.put((query, params))

    def flush_db(self):
        """Ожидание завершения всех фоновых записей в БД."""
        self._db_queue.join()

    def close(self):
        """Остановка фонового потока БД."""
        self._stop_db_event.set()
        self._db_queue.put(None)
        if self._db_thread.is_alive():
            self._db_thread.join(timeout=2.0)

    def _load_or_init_state(self) -> Dict[str, Any]:
        default_strategies = ['DLH', 'HRB', 'ARGUS', 'JAZZ']
        state = None
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except Exception as e:
                print('Error loading state:', e)

        if state is None:
            state = {
                'accounts': {},
                'positions': {},
                'pending_orders': {},
                'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
            }

        # Гарантируем присутствие всех поддерживаемых стратегий
        updated = False
        if 'accounts' not in state:
            state['accounts'] = {}
            updated = True
        if 'positions' not in state:
            state['positions'] = {}
            updated = True
        if 'pending_orders' not in state:
            state['pending_orders'] = {}
            updated = True

        for strat in default_strategies:
            if strat not in state['accounts']:
                state['accounts'][strat] = {
                    'wallet_balance': self.initial_deposit,
                    'realized_pnl': 0.0,
                    'total_fees': 0.0,
                    'trades_count': 0,
                    'wins_count': 0
                }
                updated = True
            if strat not in state['positions']:
                state['positions'][strat] = None
                updated = True
            if strat not in state['pending_orders']:
                state['pending_orders'][strat] = []
                updated = True

        if updated or not os.path.exists(self.state_path):
            self._save_state(state)

        return state

    def _save_state(self, state: Optional[Dict[str, Any]] = None):
        if state is None:
            state = self.state
        state['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def get_account(self, strategy: str) -> Dict[str, Any]:
        return self.state['accounts'].get(strategy, {})

    def get_position(self, strategy: str) -> Optional[Dict[str, Any]]:
        return self.state['positions'].get(strategy)

    def open_position(
        self,
        strategy: str,
        direction: str,
        size: float,
        price: float,
        is_maker: bool = True,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        notes: str = '',
        symbol: Optional[str] = None
    ) -> Dict[str, Any]:
        curr_pos = self.get_position(strategy)
        if curr_pos is not None:
            raise ValueError(f'Position for {strategy} already open')

        fee_rate = self.maker_fee if is_maker else self.taker_fee
        fee = price * size * fee_rate
        margin = (price * size) / self.leverage

        account = self.state['accounts'][strategy]
        account['wallet_balance'] -= fee
        account['total_fees'] += fee

        instrument = symbol or ("BTCUSDT" if strategy == "ARGUS" else self.symbol)

        pos = {
            'strategy': strategy,
            'symbol': instrument,
            'direction': direction,
            'size': size,
            'entry_price': round(price, 2),
            'entry_time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'entry_time_ms': int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
            'sl': round(sl, 2) if sl else None,
            'tp': round(tp, 2) if tp else None,
            'initial_sl': round(sl, 2) if sl else None,
            'initial_tp': round(tp, 2) if tp else None,
            'margin': round(margin, 4),
            'entry_fee': round(fee, 4),
            'notes': notes,
            'best_price': round(price, 2),
            'trailing_active': False,
            'tp1_executed': False,
            'original_size': size,
            'in_grace': False,
            'grace_start_ms': 0,
            'adverse_extremum': round(price, 2)
        }

        self.state['positions'][strategy] = pos
        self._save_state()
        return pos

    def modify_stop_loss(self, strategy: str, new_sl: float):
        pos = self.get_position(strategy)
        if pos is not None:
            pos['sl'] = round(new_sl, 2)
            self._save_state()
            return True
        return False

    def modify_take_profit(self, strategy: str, new_tp: float):
        pos = self.get_position(strategy)
        if pos is not None:
            pos['tp'] = round(new_tp, 2)
            self._save_state()
            return True
        return False

    def partial_close(self, strategy: str, size: float, price: float, reason: str = "PARTIAL_CLOSE", is_maker: bool = False):
        pos = self.get_position(strategy)
        if pos is None:
            return None
        ratio = size / pos['size']
        if ratio > 1.0: ratio = 1.0
        return self.close_position(strategy, price, reason, is_maker=is_maker, partial_ratio=ratio)

    def close_position(
        self,
        strategy: str,
        price: float,
        reason: str,
        is_maker: bool = False,
        partial_ratio: float = 1.0
    ) -> Dict[str, Any]:
        pos = self.get_position(strategy)
        if pos is None:
            raise ValueError(f'No open position for {strategy}')

        direction = pos['direction']
        full_size = pos['size']
        close_size = round(full_size * partial_ratio, 4)

        if direction == 'LONG':
            pts = price - pos['entry_price']
        else:
            pts = pos['entry_price'] - price

        gross_pnl = pts * close_size
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        exit_fee = price * close_size * fee_rate
        net_pnl = gross_pnl - exit_fee

        account = self.state['accounts'][strategy]
        account['wallet_balance'] += (gross_pnl - exit_fee)
        account['realized_pnl'] += net_pnl
        account['total_fees'] += exit_fee
        account['trades_count'] += 1
        if net_pnl > 0:
            account['wins_count'] += 1

        exit_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entry_fee = pos.get('entry_fee', 0.0) or 0.0
        total_fees = entry_fee * partial_ratio + exit_fee

        self._enqueue_db_write(
            'INSERT INTO trades (strategy, direction, size, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_gross, fees_paid, pnl_net, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (strategy, direction, close_size, pos['entry_time'], pos['entry_price'], exit_time, round(price, 2), reason, round(gross_pnl, 4), round(total_fees, 4), round(net_pnl, 4), pos.get('notes', ''))
        )
        self.flush_db()

        if partial_ratio >= 0.999:
            self.state['positions'][strategy] = None
        else:
            pos['size'] = round(full_size - close_size, 4)
            pos['tp1_executed'] = True
            self.state['positions'][strategy] = pos

        self._save_state()

        return {
            'strategy': strategy,
            'direction': direction,
            'size': close_size,
            'entry_price': pos['entry_price'],
            'exit_price': round(price, 2),
            'reason': reason,
            'gross_pnl': round(gross_pnl, 4),
            'net_pnl': round(net_pnl, 4),
            'exit_fee': round(exit_fee, 4),
            'remaining_size': pos['size'] if partial_ratio < 0.999 else 0.0,
            'wallet_balance': round(account['wallet_balance'], 4)
        }

    def check_limit_order_fill(self, order_side: str, limit_price: float, last_price: float) -> bool:
        if order_side.upper() in ('BUY', 'LONG'):
            return last_price <= (limit_price - self.limit_penetration)
        else:
            return last_price >= (limit_price + self.limit_penetration)

    def record_equity_snapshot(self, current_prices: Union[Dict[str, float], float]):
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        for strat, acc in self.state['accounts'].items():
            pos = self.state['positions'].get(strat)
            unrealized = 0.0

            if isinstance(current_prices, dict):
                inst = pos.get('symbol') if pos else ('BTCUSDT' if strat == 'ARGUS' else self.symbol)
                strat_price = current_prices.get(inst, current_prices.get(self.symbol, 0.0))
            else:
                strat_price = float(current_prices)

            if pos is not None and strat_price > 0.0:
                if pos['direction'] == 'LONG':
                    unrealized = (strat_price - pos['entry_price']) * pos['size']
                else:
                    unrealized = (pos['entry_price'] - strat_price) * pos['size']

            total_eq = acc['wallet_balance'] + unrealized
            self._enqueue_db_write(
                'INSERT INTO equity_snapshots (timestamp, strategy, wallet_balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?)',
                (now_str, strat, round(acc['wallet_balance'], 4), round(unrealized, 4), round(total_eq, 4))
            )
