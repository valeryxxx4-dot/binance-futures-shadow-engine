import re

with open('strategy_argus.py', 'r') as f:
    content = f.read()

# Fix the fallback logic so it respects the config path if it exists locally, which it does but maybe alt_path override is wrong
old_path_logic = """        model_path = self.config.get("model_path", r"d:\Telegram bot for Jarvis\www.binance.com\Argus BTC\lgb_model.txt")
        if not os.path.isabs(model_path):
            model_path = os.path.abspath(model_path)

        if not os.path.exists(model_path):
            # Fallback path lookup
            alt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Argus BTC", "lgb_model.txt")
            if os.path.exists(alt_path):
                model_path = alt_path

        self.model_path = model_path"""

new_path_logic = """        model_path = self.config.get("model_path", "models/lgb_model.txt")
        if not os.path.isabs(model_path):
            model_path = os.path.abspath(model_path)

        if not os.path.exists(model_path):
            alt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "lgb_model.txt")
            if os.path.exists(alt_path):
                model_path = alt_path

        self.model_path = model_path"""

content = content.replace(old_path_logic, new_path_logic)

with open('strategy_argus.py', 'w') as f:
    f.write(content)
