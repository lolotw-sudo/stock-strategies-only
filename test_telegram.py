from dotenv import load_dotenv
from stock_strategies.notify import send_telegram

load_dotenv()
send_telegram('🤖 Telegram 連線測試成功！')
print('✅ 訊息已發送')
