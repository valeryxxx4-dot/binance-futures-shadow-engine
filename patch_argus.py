import re

with open('strategy_argus.py', 'r') as f:
    content = f.read()

# Replace the specific model path string to avoid hardcoding issues.
if 'self.model_path = config.get("model_path", "models/lgb_model.txt")' not in content:
    old_init = 'self.model_path = r"d:\\Telegram bot for Jarvis\\www.binance.com\\Argus BTC\\lgb_model.txt"'
    new_init = 'self.model_path = self.config.get("model_path", "models/lgb_model.txt")'
    content = content.replace(old_init, new_init)

with open('strategy_argus.py', 'w') as f:
    f.write(content)
