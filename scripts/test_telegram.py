import os,sys,requests
token=os.getenv('TELEGRAM_BOT_TOKEN')
chat_id=os.getenv('TELEGRAM_CHAT_ID')
if not token or not chat_id:
    sys.exit('TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID Secret이 없습니다.')
r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
    json={'chat_id':chat_id,'text':'✅ Vintage Tee Finder 연결 테스트 성공\nGitHub Actions에서 보낸 메시지입니다.'},
    timeout=20)
r.raise_for_status()
print('Telegram test sent.')
