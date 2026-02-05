from math import log
from prog.state_store.state_store import StateStore
from prog.utils.utils import get_inverse_side
from prog.signals.ha_revers import HARevers
from prog.signals.rsi_check import RSICheck
from prog.signals.price_check import PriceCheck
from prog.managers.prta_lim_order_mng import PrtaLimOrderMng
from prog.proxy_server.proxy_driver import ProxyDriver
import logging


class Checkers:
    def __init__(self, 
        state_store: StateStore,
        prta_lim_order_mng: PrtaLimOrderMng,
        logger: logging.Logger,
        proxy_driver: ProxyDriver
    ):
        self.state_store = state_store
        self.prta_lim_order_mng = prta_lim_order_mng
        self.symbol = self.state_store.state_store_data.symbol
        self.side = self.state_store.state_store_data.side
        self.logger = logger
        self.proxy_driver = proxy_driver

        # Инициализация HA
        self.ha_avdo = HARevers(
            symbol=self.symbol, 
            side=self.side,
            logger=self.logger,
            prov_driver=self.proxy_driver,
            label="avdo"
        )
        self.logger.debug(f"Initialized HARevers for AVDO: symbol={self.symbol}, side={self.side}")
        self.ha_prta = HARevers(
            symbol=self.symbol, 
            side=get_inverse_side(self.side),
            prov_driver=self.proxy_driver,
            logger=self.logger,
            label="prta"
        )            

        # Инициализация RSI
        self.rsi_avdo = RSICheck(symbol=self.symbol, logger=self.logger, prov_driver=self.proxy_driver)
        self.rsi_avdo.start()

        self.rsi_prta = RSICheck(symbol=self.symbol, logger=self.logger, prov_driver=self.proxy_driver)
        self.rsi_prta.start()

        self.price_check = PriceCheck(
            symbol=self.symbol, 
            proxy_driver=self.proxy_driver,
            state_store=self.state_store,
            logger=self.logger
        )

    def avdo_strategy_bs(self):
        # HA check
        if not self.ha_avdo.check_revers():
            return "", "BS-Modus: HA not reversed"
        
        # RSI check
        if not self.rsi_avdo.is_snapped:
            self.logger.debug(f"RSI={self.rsi_avdo.rsi_curr}")
            return "HA, RSI", f"BS-Modus: HA reversed, but RSI not snapped"
        
        # Проверяем не пустой ли стек
        if self.state_store.stack_mng.size() == 0:
            return "average_down", "BS-Modus: HA and RSI ok, empty stack"
        
        # Получаем текущую цену
        cur_price = self.proxy_driver.get_last_price(self.symbol)

        # Получаем цену из текущего элемента стека
        cur_stack_elem = self.state_store.stack_mng.peek()
        cur_stack_elem_price = cur_stack_elem[0]

        # Проверяем ушла ли цена за последний элемент стека
        self.logger.debug(f"cur_price={cur_price}, cur_stack_elem_price={cur_stack_elem_price}")
        avdo_sell = (cur_price > cur_stack_elem_price) and (self.side == "Sell")
        avdo_buy = (cur_price < cur_stack_elem_price) and (self.side == "Buy")
        if not( avdo_sell or avdo_buy ):
            # Цена не ушла за последний элемент стека -> price check не выполняем
            return "average_down", "BS-Modus: HA and RSI ok, but Price not beyond last stack elem"
        
        # Цена ушла за последний элемент стека -> выполняем price check
        if not self.price_check.check(
            side=self.side,
            check_type="avdo"
        ):
            return "HA, RSI", "HA and RSI ok, but Price not ok"

        return "average_down", "BS-Modus: HA, RSI, Price ok"
    
    def avdo_strategy_norm(self):
        # HA check
        if not self.ha_avdo.check_revers():
            return "", "HA not reversed"
        
        # RSI check
        if not self.rsi_avdo.is_snapped:
            self.logger.debug(f"RSI={self.rsi_avdo.rsi_curr}")
            return "HA", "HA ok, but RSI not snapped"
        
        # Price check
        if not self.price_check.check(
            side=self.side,
            check_type="avdo"
        ):
            return "HA, RSI", "HA and RSI ok, but Price not ok"
        
        return "average_down", "HA  RSI, Price ok"

    def check_avdo(self):
        cur_map_elem = self.state_store.get_cur_map_elem()
        stack_size = self.state_store.stack_mng.size()
        if cur_map_elem.flag == "BS" or stack_size == 0:
            # Не проверяем условие по цене
            return self.avdo_strategy_bs()

        return self.avdo_strategy_norm()

    def reset_all_signals(self):
        cur_map_elem = self.state_store.get_cur_map_elem()
        avdo_rsi_threshold = self.state_store.state_store_data.avdo_rsi_threshold
        prta_rsi_threshold = self.state_store.state_store_data.prta_rsi_threshold
        side = self.side
        # RSI 
        self.rsi_avdo.reset(
            tf=cur_map_elem.at_rsi,             
            set_threshold=avdo_rsi_threshold,  
            side=side,
            reset_threshold=prta_rsi_threshold, 
        )
        self.rsi_prta.reset(
            tf=cur_map_elem.pt_rsi,             
            set_threshold=prta_rsi_threshold,  
            side=get_inverse_side(side),
            reset_threshold=avdo_rsi_threshold, 
        )

        # HA
        self.ha_avdo.reset(timeframe=cur_map_elem.at_ha) 
        self.ha_prta.reset(timeframe=cur_map_elem.pt_ha) 

    def check_prta(self):
        # Стек пустой?
        stack_size = self.state_store.stack_mng.size()
        if stack_size == 0:
            # Стек пустой => prta не выполняем
            return "continue"

        # Сработал ли prta_lim_order?
        if self.state_store.data.is_limit_order_filled:
            return "profit_take_lim"

        # Проверяем условия prta
        if self.check_prta_price_ha_rsi():
            return "profit_take_market"
        
        return "continue"
        
    def check_prta_price_ha_rsi(self):
        # HA check
        ha = self.ha_prta.check_revers()

        # RSI check
        rsi = self.rsi_prta.is_snapped

        # Price check
        price = self.price_check.check(
            side=get_inverse_side(self.side),
            check_type="prta"
        )

        #print(f"prta ha={ha}, rsi={rsi}, price={price}")

        if ha and rsi and price:
            return True

        return False




