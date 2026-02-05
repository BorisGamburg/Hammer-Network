from prog.signals.ha_revers import HARevers
from pprint import pprint
from prog.proxy_server.proxy_driver import ProxyDriver


class TrailingStopLoss:
    """
    Класс для управления скользящим стоп-лоссом через условные ордера Bybit.
    
    Особенности:
    - Работает с отдельными SL-ордерами (поддержка нескольких SL на позицию)
    - Автоматически подтягивает SL при движении цены в прибыльную сторону
    - Отслеживает ручные изменения SL и адаптирует отступ
    - Завершается при срабатывании всех SL-ордеров
    - Поддерживает частичное закрытие позиции
    
    Параметры:
    - bybit_driver: экземпляр BybitDriver
    - symbol: торговая пара (например, "BTCUSDT")
    - side: сторона позиции ("Buy" или "Sell")
    - sl_qty: размер позиции для SL (None = вся позиция)
    - trail_percent: начальный процент отступа (например, 0.02 = 2%)
    - trail_amount: начальная абсолютная величина отступа
    - initial_sl_price: начальная цена SL (если None, рассчитывается от текущей цены)
    - order_link_id: уникальный ID для отслеживания ордера (опционально)
    - poll_interval: интервал проверки в секундах
    """
    
    def __init__(
        self,
        symbol: str,
        proxy_driver: ProxyDriver,
        logger=None,
        pos_side=None,
        sl_qty_factor=None,
        timeframe=None,
    ):
        self.logger = logger
        self.proxy_driver = proxy_driver
        self.symbol = symbol
        self.pos_side = pos_side
        self.sl_qty_factor = sl_qty_factor
        self.timeframe = timeframe

        self.poll_interval = round(int(self.timeframe) * 60 / 5)
        
        # Внутренние переменные
        self.sl_order_id = None
        self.current_sl_price = None
        self.is_running = False
        self.position_idx = 1 if pos_side == "Buy" else 2

        # Инициализация HARevers для анализа свечей
        self.ha_rev = HARevers(
            symbol=self.symbol,
        )

        response = self.proxy_driver.execute("get_symbol_info", symbol=self.symbol)
        instrument_info = response['result']['list'][0]
        self.price_step = float(instrument_info['priceFilter']['tickSize'])  # 0.0001

    def _get_position_size(self):
        """Получает текущий размер позиции."""
        buy_size, sell_size, _, _, _, _ = self.proxy_driver.execute("get_position_data", symbol=self.symbol)
        
        if self.pos_side == "Buy":
            return buy_size
        else:
            return sell_size
        
    def _calculate_sl_price(self, reference_price):
        """Рассчитывает цену SL от предпоследней HA-свечи (V20)."""
        
        # 1. Получаем данные от сервера через драйвер
        data = self.proxy_driver.get_data(self.symbol, tf=self.timeframe)
        
        # 2. Достаем объект предпоследней свечи
        prev_ha = data.get('prev_ha')
        
        # 3. Выбираем уровень SL в зависимости от направления
        # Используем ключи, которые ты прописал в сервере: HA_low и HA_high
        if self.pos_side == "Buy":
            sl_price = prev_ha['HA_low']
        else:
            sl_price = prev_ha['HA_high']
            
        self.logger.info(f"SL рассчитан по данным сервера: {sl_price} (Ref: {reference_price})")
        return sl_price
    
    def _find_my_sl_order(self):
        """Ищет наш SL-ордер среди активных."""
        try:
            active_orders = self.proxy_driver.execute("get_active_orders", symbol=self.symbol)
            for order in active_orders:
                # ищем по orderId 
                if order["order_id"] == self.sl_order_id:
                    return order
            return None
        except Exception as e:
            self.logger.error(f"Ошибка при поиске SL-ордера: {str(e)}")
            return None
    
    def _place_sl_order(self, sl_price, qty):
        """Выставляет условный SL-ордер (Stop Market)."""
        try:
            # Определяем сторону для закрытия
            close_side = "Sell" if self.pos_side == "Buy" else "Buy"
            
            # Округляем цену
            sl_price_valid = self.proxy_driver.execute("round_to_step", price=sl_price, step=self.price_step)

            # Округляем количество
            qty_valid = self.proxy_driver.execute("get_valid_order_qty", symbol=self.symbol, qty=qty)
            
            # Выставляем условный ордер
            response = self.proxy_driver.execute("place_trigger_order",
                symbol=self.symbol,
                side=close_side,
                qty=str(qty_valid),
                trigger_price=str(sl_price_valid),
                position_idx=self.position_idx
            )
            
            if response.get("retCode") == 0:
                order_id = response["result"]["orderId"]
                self.logger.info(f"✅ TSL-ордер выставлен: price={sl_price_valid} ID={order_id}")
                return order_id
            else:
                self.logger.error(f"❌ Ошибка выставления SL: {response.get('retMsg')}")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ Исключение при выставлении SL: {str(e)}")
            return None
    
    def _update_sl_order(self, new_sl_price):
        """Обновляет существующий SL-ордер через amend_order."""
        try:
            if not self.sl_order_id:
                self.logger.error("❌ Нет ID ордера для обновления")
                return False
            
            # Округляем цену
            new_sl_price = self.proxy_driver.execute("round_to_step", price=new_sl_price, step=self.price_step)

            # 2. ПРОВЕРКА: Если цена та же самая — ничего не делаем
            if self.current_sl_price and abs(new_sl_price - self.current_sl_price) < self.price_step:
                # self.logger.debug("TSL: Цена не изменилась, пропуск обновления.")
                return True

            response = self.proxy_driver.execute("amend_order",
                symbol=self.symbol,
                orderId=self.sl_order_id,
                new_price=str(new_sl_price)
            )
            if response.get("retCode") == 0:
                self.current_sl_price = new_sl_price
                self.logger.info(f"✅ TSL обновлен на {new_sl_price:.4f}")
                return True
            else:
                self.logger.info(f"❌ TSL не обновлен. Возможно исполнен.")
                return False
        except Exception as e:
            self.logger.error(f"❌ Исключение при обновлении TSL: {str(e)}")
            return False
    
    def _initialize_sl(self):
        """Инициализирует SL-ордер."""
        # Получаем текущую позицию
        position_size = self._get_position_size()
        
        if position_size <= 0:
            raise Exception("Позиция не найдена")
        
        # Определяем объем SL
        sl_qty = self.sl_qty_factor * position_size

        # Получаем текущую цену
        current_price = self.proxy_driver.get_last_price(self.symbol)
        
        # Рассчитываем цену SL
        sl_price = self._calculate_sl_price(current_price)
        
        self.logger.info(f"📊 Инициализация TSL:")
        self.logger.info(f"   Позиция: {self.pos_side} {position_size}")
        self.logger.info(f"   TSL qty: {sl_qty}")
        self.logger.info(f"   Текущая цена: {current_price:.4f}")
        self.logger.info(f"   TSL price: {sl_price:.4f}")

        # Выставляем TSL
        order_id = self._place_sl_order(sl_price, sl_qty)
        if not order_id:
            raise Exception("Не удалось выставить начальный TSL")

        self.sl_order_id = order_id
        self.current_sl_price = sl_price
    
    def _check_and_update_trailing(self):
        """Проверяет и обновляет trailing stop."""
        # Получаем текущую цену
        current_price = self.proxy_driver.get_last_price(self.symbol)
        
        # Проверяем, существует ли ещё наш ордер
        existing_order = self._find_my_sl_order()
        if not existing_order:
            self.logger.warning("⚠️ TSL-ордер не найден (возможно, сработал)")
            return False

        # Расчитываем SL price на основе HA-свечей        
        new_sl_price = self._calculate_sl_price(current_price)
        
        # Пробуем обновить через amend_order
        success = self._update_sl_order(new_sl_price)
        
        # Если не получилось, выходим
        if not success:
            return False

        return True
    
    def stop(self):
        # Убирает trailing stop loss ордер
        if self.sl_order_id:
            try:
                self.proxy_driver.execute("cancel_order", symbol=self.symbol, order_id=self.sl_order_id)
                # При отсутствии ордера ислючение не выбрасывается
                #self.logger.info(f"❌ ТSL-ордер {self.sl_order_id} отменен")
            except Exception as e:
                self.logger.info(f"❌ TSL-ордер не мог быть отменен: {str(e)}")

    
    def get_status(self):
        """Возвращает текущий статус."""
        return {
            "is_running": self.is_running,
            "symbol": self.symbol,
            "side": self.pos_side,
            "sl_order_id": self.sl_order_id,
            "current_sl_price": self.current_sl_price,
            "trail_percent": self.trail_percent,
            "trail_amount": self.trail_amount
        }

    def _run_tsl_with_interrupt(self, stop_event):
        """Запуск TSL с поддержкой прерывания через stop_event."""
        self.is_running = True
        
        try:
            # Инициализация
            self._initialize_sl()
            
            # Основной цикл с проверкой stop_event
            while self.is_running and not stop_event.is_set():
                # Обновляем trailing stop
                if not self._check_and_update_trailing():
                    break
                
                # Прерываемое ожидание вместо sleep
                if stop_event.wait(timeout=self.poll_interval):
                    self.logger.debug("⏸️ TSL: получен сигнал остановки")
                    break
                    
        except Exception as e:
            self.logger.error(f"❌ Критическая ошибка TSL: {str(e)}", exc_info=True)
        finally:
            self.stop()
            self.is_running = False
            self.logger.debug("🛑 Trailing Stop Loss завершен")

    def set_stop_loss(self, 
        symbol: str, 
        side: str, 
        qty: float,       
        price: float
    ):
        position_idx = 2 if side == 'Buy' else 1
        response = self.place_trigger_order(
            symbol=symbol,
            side=side,     
            position_idx=position_idx,
            qty=qty,               
            trigger_price=price   
        )

        if response and response.get('retCode') == 0:
            self.logger.info(f"API: SL на {qty:.4f} для позиции {side} установлен на {price:.2f}.")
        else:
            raise Exception(f"Ошибка при установке SL через API: {response}")
            


