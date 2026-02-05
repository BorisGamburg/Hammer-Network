import logging
import threading
from pprint import pprint
from prog.state_store.state_store import StateStore 
from prog.state_store.state_data_schema import StateDataSchema 
from prog.trade_bot.tb import TradeBot
from .tsl_manager import TSLManager
from prog.utils.utils import get_inverse_side, remove_state_file, log_parameters
from prog.trade_over_bot import bootstrap 
from .handlers import Handlers    

class TradeOverBot:
    # Явное объявление типов для IDE и линтеров
    state_store: StateStore
    tb: TradeBot
    handlers: Handlers
    tsl_mgr: TSLManager

    def __init__(self, config_tag: str):
        # 1. Базовая конфигурация
        self.config_file_path = bootstrap.full_config_path
        self.logger = bootstrap.logger
        self.telegram = bootstrap.telegram
        self.proxy_driver = bootstrap.proxy_driver
        self.shutdown_event = threading.Event()
        self.state_dir = bootstrap.state_dir
        self.config_tag = config_tag

        # 2. Инициализация StateStore (сразу загружает данные)
        self.state_store = StateStore(
            config_file=self.config_file_path,
            logger=self.logger,
        )

        # Настройка логгера на основе данных из конфига
        if self.state_store.data.debug:
            self.logger.setLevel(logging.DEBUG)

        # 3. Инициализация trade bot
        self.tb = TradeBot(
            config_tag=self.config_tag,
            state_store=self.state_store,
        )

        # 4. Инициализация handlers
        self.handlers = Handlers(
            proxy_driver=self.proxy_driver,
            state_store=self.state_store,
            logger=self.logger,
        )        
        
        # 5. Инициализация TSL Manager
        self.tsl_mgr = TSLManager(
            logger=self.logger,
            proxy_driver=self.proxy_driver,
            state_store_data=self.state_store.data, 
            telegram=self.telegram,
            shutdown_event=self.shutdown_event,
        )

        # Логирование параметров при инициализации
        log_parameters(self)

    def run(self) -> None:
        self.tsl_mgr.start_if_needed()

        iteration = 1
        try:
            while not self.shutdown_event.is_set():
                # Log
                self.logger.info(f"\n{'=' * 60}")
                self.logger.info(f"Итерация {iteration}")

                # Запускаем основной цикл TradeBot
                result = self.tb.run()       

                # Обрабатываем результаты          
                self.handlers.handle(result)

                #
                iteration += 1

        except KeyboardInterrupt:
            self.logger.info("Остановлено пользователем")
        except Exception as e:
            self.logger.exception(f"Критическая ошибка: {e}")
            self.telegram.send_telegram_message(f"{self.state_store.data.symbol} | Ошибка: {e}")
        finally:
            self.stop()

    def stop(self) -> None:
        self.logger.info("Остановка TradeOverBot...")

        # Завершаем работу
        self.shutdown_event.set()

        # Останавливаем TSL Manager
        self.tsl_mgr.stop()

        # Сохраняем состояние при остановке
        self.state_store.save()
             
        # remove_state_file(
        #     logger=self.logger,
        #     config_tag=self.config_tag,
        #     state_dir=self.state_dir
        # )        

        self.logger.info("TradeOverBot остановлен.")