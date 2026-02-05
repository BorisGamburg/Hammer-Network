import threading
from typing import Optional, Any
import logging 
from prog.state_store.state_data_schema import StateDataSchema
from .tsl import TrailingStopLoss
from prog.proxy_server.proxy_driver import ProxyDriver

# ❗ ПРИМЕЧАНИЕ: Предполагается, что TrailingStopLoss может быть импортирован 

class TSLManager:
    def __init__(
        self, 
        logger: logging.Logger,
        proxy_driver: ProxyDriver,
        state_store_data: StateDataSchema,    # Единственный Источник Правды (SSOT)
        telegram: Any,              # Для отправки сообщений
        shutdown_event: threading.Event, # Для остановки потока
    ):
        # ❗ ПРИСВАИВАЕМ ВСЕ СЕРВИСЫ/ЗАВИСИМОСТИ
        self.logger = logger
        self.state_store_data = state_store_data
        self.telegram = telegram
        self.shutdown_event = shutdown_event 
        self.proxy_driver = proxy_driver
        self.symbol = self.state_store_data.symbol
        self.side = self.state_store_data.side
        self.tsl_qty_factor = self.state_store_data.tsl_qty_factor
        self.tsl_timeframe = self.state_store_data.tsl_timeframe
        
        self.thread: Optional[threading.Thread] = None

    def start_if_needed(self) -> None:
        if self.tsl_qty_factor != 0:
            self.thread = threading.Thread(
                target=self._worker,
                name=f"TSL-{self.symbol}",
                daemon=False,
            )
            self.thread.start()
            self.logger.info(f"TrailingStopLoss запущен для {self.symbol}")

    def _worker(self) -> None:
        try:
            # Все параметры для TrailingStopLoss берутся из self.<param>, 
            # которые были инициализированы из SSOT-конфигурации
            tsl = TrailingStopLoss( 
                logger=self.logger,     
                proxy_driver=self.proxy_driver,        
                symbol=self.symbol,             
                pos_side=self.side,             
                sl_qty_factor=self.tsl_qty_factor, 
                timeframe=self.tsl_timeframe,   
            )
            tsl._run_tsl_with_interrupt(self.shutdown_event) 
            
            # ❗ Корректный вызов ConfigManager:
            self.state_store_mng.set_config_param("tsl_qty_factor", None, None) 
            mes = f"{self.symbol} — TSL завершён."
            self.logger.info(mes)
            self.telegram.send_telegram_message(mes)
        except Exception as e:
            self.logger.error(f"Ошибка в потоке TSL: {e}")

    def stop(self) -> None:
        if self.thread and self.thread.is_alive():
            self.logger.info("Остановка Trailing Stop Loss...")
            self.shutdown_event.set() 
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                self.logger.warning("TSL поток не завершился вовремя!")