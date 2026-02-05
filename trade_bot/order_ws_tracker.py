from pybit.unified_trading import WebSocket


class OrderWSTracker:
    def __init__(self, 
                 api_key: str, 
                 api_secret: str, 
                 callback_tp_sl=None, 
                 callback_order=None, 
                 logger=None):
        """
        Initializes the BybitOrderTracker.

        Args:
            api_key (str): Your Bybit API key.
            api_secret (str): Your Bybit API secret.
            testnet (bool): Set to True for testnet, False for mainnet. Defaults to False.
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.callback_tp_sl = callback_tp_sl
        self.callback_order = callback_order
        self.logger = logger 

        self.ws = None
        self.logger.debug("OrderManager initialized.")

    def _handle_message(self, message: dict):
        """
        Processes incoming WebSocket messages.

        Args:
            message (dict): The message received from the WebSocket.
        """
        try:
            if "topic" in message and message["topic"] == "order":
                for order in message["data"]:
                    if order.get("orderStatus") == "Filled":
                        # проверяем, что это TP
                        if (order.get("stopOrderType") == "TakeProfit") and (order.get("tpslMode") == "Full"):
                            self.logger.info(f"✅ ПолныйTP сработал! Ордер: {order['orderId']}")
                            if self.callback_tp_sl:
                                self.callback_tp_sl(order)
                        # проверяем, что это SL
                        elif (order.get("stopOrderType") == "StopLoss") and (order.get("tpslMode") == "Full"):
                            self.logger.info(f"❌ Полный SL сработал! Ордер: {order['orderId']}")
                            if self.callback_tp_sl:
                                self.callback_tp_sl(order)
                        else:
                            if self.callback_order:
                                self.callback_order(order)
            else:
                self.logger.debug(f"Получено сообщение: {message}")

        except Exception as e:
            self.logger.error(f"Ошибка при обработке сообщения: {e}")

    def start(self):
        """Starts the WebSocket connection and subscribes to the order stream."""
        if self.ws:
            self.logger.warning("WebSocket already running. Please stop it first.")
            return

        self.ws = WebSocket(
            testnet=False,
            channel_type="private",
            api_key=self.api_key,
            api_secret=self.api_secret
        )
        self.ws.order_stream(callback=self._handle_message)



