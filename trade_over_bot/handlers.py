import logging
from prog.trade_bot.tb import TradeBot
from prog.state_store.state_store import StateStore
from prog.proxy_server.proxy_driver import ProxyDriver
from prog.managers.chase_mng import ChaseManager
from prog.state_store.stack_mng import StackManager
from prog.state_store.state_data_schema import AllowedTimeframes
from typing import cast


class Handlers:
    def __init__(
        self, 
        state_store: StateStore,       
        logger: logging.Logger,    
        proxy_driver: ProxyDriver
    ):
        self.state_store = state_store
        self.stack_mng: StackManager = state_store.stack_mng
        self.map_mng = state_store.map_mng
        self.logger = logger
        self.proxy_driver = proxy_driver
        
        state_store_data = self.state_store.data
        
        # Конфигурационные параметры
        self.symbol = state_store_data.symbol 
        self.side = state_store_data.side 
        self.prta_min_offset = state_store_data.prta_offset_min_pct/100 

        self.chase_mng = ChaseManager(proxy_driver, logger)
        
    def handle(self, result: str) -> None:
        cur_price = self.proxy_driver.get_last_price(self.symbol) 
        if result == "average_down":
            self.handle_average_down()
        elif result == "profit_take_market":
            self.handle_profit_take_market(cur_price)
        elif result == "profit_take_lim":
            self.handle_profit_take_limit()
        else:
            raise ValueError(f"Неизвестный результат от TradingBot: {result}")

    def handle_average_down(self) -> None:
        # Выставляем chase order
        qty, order_id = self.place_chase_order()

        # Получаем информацию по исполненному ордеру
        order_price = self.save_order_on_stack(qty, order_id)

        # Сохраняем state_store в файл
        self.state_store.save()

        # Log
        self.logger.info(f"Average Down выполнен: Qty={qty}, Price={order_price}")

        # Проверяем необходимость сброса avdo_tf по флагу RS
        if self.handle_RS():
            self.logger.info("AvDo таймфреймы сброшены по флагу RS")

    def save_order_on_stack(self, qty, order_id):
        order_info = self.proxy_driver.execute("get_executed_order_info", order_id=order_id)
        if order_info is None:
            raise ValueError(f"Не удалось получить информацию по ордеру {order_id}")

        # Получаем цену исполнения ордера
        order_price = float(order_info.get('avgPrice'))

        # Запоминаем ордер на стеке
        if self.stack_mng:
            self.stack_mng.push(order_price, qty)
        else:
            raise RuntimeError("StackManager не инициализирован!")

        return order_price

    def place_chase_order(self):
        cur_map_elem = self.state_store.get_cur_map_elem()

        # Рассчитываем qty_factor
        qty_factor = cur_map_elem.qty_pct / 100  

        # Получаем qty для avdo ордера
        qty = self.proxy_driver.execute("get_valid_qty", symbol=self.symbol, qty_factor=qty_factor) 

        # Выставляем chase order
        order_id = self._place_chase_order(qty)
        return qty,order_id

    def _place_chase_order(self, qty):
        # Выставляем chase order 
        response = self.chase_mng.wait_chase_order(
            symbol=self.symbol,
            side=self.side,
            qty=qty,
            pool_dist_threshold_pct=self.state_store.data.pool_dist_threshold_pct,
            sl_ratio=None,
            exclude_patterns=[],
        )

        # Проверяем результат
        if response is None:
            raise ValueError("Не удалось выставить chase order.")
        
        # Извлекаем order_id
        res, order_id = response

        # Возвращаем order_id
        return order_id

    def handle_profit_take_market(self, cur_price: float) -> None:
        if self.stack_mng is None:
            raise RuntimeError("StackManager не инициализирован!")

        while not self.stack_mng.is_empty():
            # 1. Вызываем peek() ОДИН раз и сохраняем результат
            top_element = self.stack_mng.peek()
            
            # 2. Проверяем сохраненную переменную
            if top_element is None:
                self.logger.warning("Не удалось получить верхний элемент стека.")
                break
                
            # 3. Распаковываем переменную (теперь Pylance спокоен)
            stack_top_price, stack_top_qty = top_element
            stack_top_price = float(stack_top_price)
            
            # Рассчитываем offset
            offset = (cur_price - stack_top_price) / cur_price * (-1 if self.side == "Sell" else 1)
            
            # Сравниваем offset с минимальным
            if offset <= self.prta_min_offset: 
                break
            
            # Устанавливаем side и pos_idx
            side_inverse = "Buy" if self.side == "Sell" else "Sell"
            pos_idx = 2 if side_inverse == "Buy" else 1

            # Выполняем рыночный ордер
            self.proxy_driver.execute("place_market_order", symbol=self.symbol, side=side_inverse, pos_idx=pos_idx, qty=stack_top_qty) 
            # Кoрректируем соответственно стек
            self.stack_mng.pop()
            # Сохраняем state_store в файл
            self.state_store.save()

            # Log
            self.logger.info(
                f"Profit Take Market: {side_inverse} {stack_top_qty} @ {cur_price} "
                f"(закрыта позиция по {stack_top_price}) | Осталось: {self.stack_mng.size()}"
            )


    def handle_profit_take_limit(self):
        # 1. Проверяем флаг (он никогда не None, так что это безопасно)
        if self.state_store.data.is_limit_order_filled:
            
            # 2. Уменьшаем стек (логика, которая была раньше)
            self.stack_mng.pop()
            
            # 3. Сбрасываем флаг в False
            self.state_store.data.is_limit_order_filled = False
            
            # 4. Сохраняем. Теперь save() пройдет как по маслу.
            self.state_store.save()
            
            self.logger.info("✅ Исполнение лимитки обработано, стек уменьшен.")


    def handle_RS(self):
        # Проверяем установлен ли сброс avdo_tf

        # Получаем текущий map элемент
        cur_map_elem = self.state_store.get_cur_map_elem()
        flag = cur_map_elem.flag
        if flag is not None and flag[:2] == "RS":                
            # Сброс avdo_tf установлен
            # Получаем tf для сброса
            reset_tf = int(flag[2:])

            # Приводим тип к нашему общему Literal
            valid_tf = cast(AllowedTimeframes, reset_tf)

            # Теперь Pylance не будет ругаться на несовпадение типов
            cur_map_elem.at_rsi = valid_tf
            cur_map_elem.at_ha = valid_tf
            
            # Записываем state_store в файл
            self.state_store.save()





