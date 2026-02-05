import time
from ks.keys import DB_PASSWORD, API_KEY
from prog.state_store.state_store import StateStore 
from prog.managers.prta_lim_order_mng import PrtaLimOrderMng
from .state_writer import write_state_to_file
from .order_ws_tracker import OrderWSTracker
from .checker import Checkers
import prog.trade_over_bot.bootstrap as bootstrap
from prog.state_store.state_data_schema import FilledOrderInfo

class TradeBot:

    def __init__(
        self, 
        config_tag: str,
        state_store: StateStore,
    ):
        # Переменные класса
        self.logger = bootstrap.logger
        self.telegram = bootstrap.telegram
        self.state_store = state_store 
        self.config_tag=config_tag
        self.state_dir=bootstrap.state_dir
        self.loss_threshold_alerted = False # Состояние для логирования порога убытка (чтобы не спамить)
        
        # Удобная ссылка на сырые данные Pydantic
        self.state_data = self.state_store.data

        # Создание PrtaLimOrderMng
        self.prta_lim_order_mng = PrtaLimOrderMng(
            logger=self.logger,
            proxy_driver=bootstrap.proxy_driver,
            state_store=self.state_store 
        )        

        # Создаем Checkers
        self.checkers = Checkers(
            state_store=self.state_store,
            prta_lim_order_mng=self.prta_lim_order_mng,
            logger=self.logger,
            proxy_driver=bootstrap.proxy_driver
        )

        # Состояние
        self.tp_sl_filled = False

        # Создание OrderWSTracker
        self.order_man = OrderWSTracker(
            api_key=API_KEY, 
            api_secret=DB_PASSWORD, 
            logger=bootstrap.logger,
            callback_order=self.callback_order_filled,
            callback_tp_sl=self.callback_tp_sl_filled,
        )
        
        # Запуск OrderManager!
        self.order_man.start()   

    def _get_position_and_equity(self) -> tuple[float, float]:
        """
        Получает текущий эквити и анреализованный PnL позиции.
        
        :return: Кортеж (total_equity, current_unpnl)
        """
        # Получаем данные через твой драйвер
        total_equity = bootstrap.proxy_driver.execute("get_total_equity")
        if total_equity is None:
            raise Exception("Не удалось получить данные по эквити.")

        # 1. Получаем данные
        pos_data = bootstrap.proxy_driver.execute(
            "get_position_data", 
            symbol=self.state_data.symbol
        )

        # 2. Проверяем, что данные пришли (не None)
        if pos_data is None:
            raise Exception("Не удалось получить данные по позиции.")

        # 3. Безопасно распаковываем (теперь Pylance не будет ругаться)
        _, _, buy_unpnl, sell_unpnl, _, _ = pos_data

        current_unpnl = buy_unpnl if self.state_data.side == "Buy" else sell_unpnl
        
        return float(total_equity), float(current_unpnl)

    def _is_drawdown_reached(self) -> tuple[bool, float]:
        """
        Проверяет, достигнут ли порог убытка.   
        :return: Кортеж (drawdown_reached, cur_loss_pct)
        """
        # Получаем эквити и анреализованный PnL
        total_equity, current_unpnl = self._get_position_and_equity()

        # Считаем текущий % убытка
        cur_loss_pct = (current_unpnl / total_equity) * 100
        
        # Сравниваем с "Пределом убытка" из конфига
        loss_threshold = -abs(self.state_data.loss_limit_pct)
        if cur_loss_pct <= loss_threshold:
            return True, cur_loss_pct

        return False, cur_loss_pct
    
    def _should_bot_be_active(self) -> tuple[bool, float]:
        """
        Определяет, должен ли бот работать в быстром цикле 
        или уходить в глубокий сон (5 мин).
        """
        # 1. ПРОВЕРКА ПРОСАДКИ (Порог входа)
        drawdown_reached, cur_loss_pct = self._is_drawdown_reached()

        # 2. ПРОВЕРКА СТЕКА (Порог выхода)
        is_stack_not_empty = self.state_store.stack_mng.size() > 0

        # ЛОГИЧЕСКОЕ "ИЛИ"
        return (drawdown_reached or is_stack_not_empty), cur_loss_pct             

    def _handle_loss_check(self) -> bool:
        """
        Обрабатывает логику проверки просадки.
        Возвращает True если нужно продолжить цикл (спать),
        False если нужно продолжить нормальную работу.
        """
        if not self.state_data.is_loss_check:
            return False
        
        is_active, cur_loss_pct = self._should_bot_be_active()
        
        if not is_active:
            # Если мы только что вернулись в норму (или только запустились в норме)
            if self.loss_threshold_alerted:
                self.logger.info(f"✅ Убыток в норме (менее {self.state_data.loss_limit_pct}%). Убыток {cur_loss_pct:.2f}%. Переходим в режим сна.")
                self.loss_threshold_alerted = False
            
            time.sleep(300)
            return True  # Продолжаем цикл (спим)
        else:
            # Если порог пробит ВПЕРВЫЕ (или стек стал не пустым)
            if not self.loss_threshold_alerted:
                self.logger.warning(f"⚠️ ВНИМАНИЕ: Порог убытка {self.state_data.loss_limit_pct}% достигнут! Бот переходит в АКТИВНЫЙ режим.")
                self.loss_threshold_alerted = True
            
            return False  # Продолжаем нормальную работу

    def _handle_average_down_check(self):
        """
        Проверяет и обрабатывает условие усреднения вниз.
        Возвращает код выхода (если произошло усреднение) и s2 для логирования.
        """
        s1, s2 = self.checkers.check_avdo()
        
        if s1 == "average_down":
            # Закрываем старый prta lim order
            self.prta_lim_order_mng.cancel_prta_lim_order()
            return "average_down", None
        
        return None, s2

    def _handle_profit_take_check(self):
        """
        Проверяет и обрабатывает условие взятия профита.
        Возвращает код выхода (если произошло усреднение или лимитный профит) или None для продолжения.
        """
        res = self.checkers.check_prta()
        
        if res == "profit_take_market":
            # Закрываем старый prta lim order
            self.prta_lim_order_mng.cancel_prta_lim_order()
            return "profit_take_market"
        elif res == "profit_take_lim":
            return "profit_take_lim"
        
        return None

    def _run(self) -> str:
        # Устанавливаен base_cond_price в price_check
        base_cond_price = self.state_store.get_base_cond_price()
        self.checkers.price_check.set_base(base_cond_price)

        # Выставляем лимитный ордер на профит
        self.prta_lim_order_mng.check_place_prta_lim_order()  
        
        # Сброс всех сигналов в начале нового цикла стека
        self.checkers.reset_all_signals() 

        # Log
        self.logger.info("stack: %s", self.state_store.state_store_data.stack)
        self.logger.info("stack_size: %s", self.state_store.stack_mng.size())
        self.logger.info("cur_map_elem: %s", self.state_store.get_cur_map_elem())
        self.logger.info(f"Текущая цена: {bootstrap.proxy_driver.get_last_price(self.state_data.symbol)}")


        if self.state_data.is_loss_check:
            is_active, cur_loss_pct = self._should_bot_be_active()

            # Сразу выводим стартовый статус
            msg = "⚠️ АКТИВНЫЙ (порог пробит)" if is_active else "💤 СПЯЩИЙ (убыток в норме)"
            self.logger.info(f"Режим {msg} | Порог: {self.state_data.loss_limit_pct}%, Текущий убыток: {cur_loss_pct:.2f}%")

            # Инициализируем флаг уведомления текущим состоянием, 
            # чтобы не дублировать сообщения в цикле
            self.loss_threshold_alerted = is_active

        # Основной бесконечный цикл
        while True:
            if self.tp_sl_filled:
                raise RuntimeError("TP или SL сработал — завершаем работу TradeBot")
            
            # Проверка просадки включена?
            if self._handle_loss_check():
                continue

            # --- ШТАТНАЯ РАБОТА ---
            # Если мы здесь, значит либо is_loss_check=False (работаем всегда),
            # либо порог пробит / стек не пуст (пора работать).

            # Check AVERAGING DOWN
            exit_code, s2 = self._handle_average_down_check()
            if exit_code:
                return exit_code

            # Check PROFIT TAKE
            exit_code = self._handle_profit_take_check()
            if exit_code:
                return exit_code

            # Записываем состояние в файл
            write_state_to_file(
                self.config_tag, 
                str(self.state_dir),
                self.state_store,
                self.checkers,
                str(s2)
            )            

            # Спим до следующей итерации
            time.sleep(self.get_sleep_interval()) 

    def get_sleep_interval(self) -> int:
        cur_map_elem = self.state_store.get_cur_map_elem()
        tfs = [
            int(cur_map_elem.at_rsi), 
            int(cur_map_elem.at_ha), 
            int(cur_map_elem.pt_rsi), 
            int(cur_map_elem.pt_ha),  
        ]
        min_tfs = min(tfs)
        if min_tfs == 1:
            interval = 20
        elif min_tfs in [3,5]:
            interval = 30
        elif min_tfs in [10, 15, 30]:
            interval = 60
        else:
            interval = 120
            
        #print(f"interval={interval}")
        return interval

    def run(self) -> str:
        """
        Публичный метод с ГАРАНТИРОВАННОЙ очисткой ордера при любом выходе.
        """
        
        result = ""
        
        try:
            result = self._run()
            return result

        finally:
            self.prta_lim_order_mng.cancel_prta_lim_order()

    def callback_order_filled(self, order: dict):
        if order.get("orderLinkId") == self.prta_lim_order_mng.prta_lim_order_link_id:
            # Обновляем данные ордера
            self.state_store.data.last_filled_limit_order = FilledOrderInfo(
                orderId=str(order.get("orderId", "")),
                side=str(order.get("side", "")),
                qty=str(order.get("qty", "")),
                price=str(order.get("avgPrice") or order.get("price") or "")
            )
            
            # Поднимаем ЕДИНСТВЕННЫЙ флаг
            self.state_store.data.is_limit_order_filled = True
            
            # Сохраняем в файл
            self.state_store.save()
            self.logger.info(f"✅ Лимитка {order.get('orderId')} исполнена. Состояние сохранено.")            

    def callback_tp_sl_filled(self, order: dict):
        """
        Вызывается OrderManager при срабатывании стоп-ордера (TP или SL).
        Сигнализирует о полном завершении позиции.
        """
        if order.get("symbol") == self.state_data.symbol:
            # Устанавливаем критический флаг, который остановит цикл run()
            self.tp_sl_filled = True 
            self.logger.critical(f"❌ Полный TP или SL сработал для {order.get('symbol')}! Устанавливаем флаг завершения.")

