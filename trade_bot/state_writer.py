from pathlib import Path 
import json
from prog.state_store.state_store import StateStore
from prog.trade_bot.checker import Checkers 


def write_state_to_file(
    config_tag: str, 
    state_dir: str,
    state_store: StateStore,
    checkers: Checkers,
    s2: str
) -> None:
    """
    Сохраняет текущее состояние бота в файл STATE/{config_tag}.log.
    """
    
    file_path = Path(state_dir) / f"{config_tag}.log"
    
    # 2. Формируем словарь состояния
    stack_str = state_store.stack_mng.to_string()
    cur_stack_size = state_store.stack_mng.size()
    rsi = checkers.rsi_avdo.rsi_curr
    rsi_snapped = checkers.rsi_avdo.is_snapped

    state = {
        "stack_str": stack_str,
        "stack_size": cur_stack_size,
        "tsl_status": "OFF", 
        "avdo_status": s2,
        "rsi": rsi,
        "rsi_snapped": rsi_snapped
    }
    
    # 3. Записываем файл
    try:
        with open(file_path, "w") as f:
            json.dump(state, f, indent=4)
    except IOError as e:
        # В рабочем коде это должно быть заменено на log.error
        print(f"Ошибка записи файла состояния {file_path}: {e}")