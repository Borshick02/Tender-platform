#!/bin/bash
# Автоматическая установка Мультипарсера Тендеров на Linux сервер

set -e

echo "════════════════════════════════════════════════════════════"
echo "  УСТАНОВКА МУЛЬТИПАРСЕРА ТЕНДЕРОВ НА СЕРВЕР"
echo "════════════════════════════════════════════════════════════"
echo ""

# Проверка прав root
if [ "$EUID" -ne 0 ]; then 
    echo "❌ Запустите скрипт от имени root:"
    echo "   sudo bash install_server.sh"
    exit 1
fi

echo "[1/9] Обновление системы..."
apt update -qq
apt upgrade -y -qq

echo "[2/9] Установка Python и зависимостей..."
apt install -y python3 python3-pip python3-venv \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    git screen -qq

echo "[3/9] Создание директории проекта..."
mkdir -p /opt/tenderparser
cd /opt/tenderparser

echo "[4/9] Создание виртуального окружения..."
python3 -m venv venv
source venv/bin/activate

echo "[5/9] Обновление pip..."
pip install --upgrade pip -q

echo "[6/9] Установка Python зависимостей..."
pip install -r requirements.txt -q

echo "[7/8] Установка Playwright Chromium..."
playwright install chromium
playwright install-deps

echo "[8/8] Настройка firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 8000/tcp
    ufw reload
    echo "✓ UFW настроен"
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --permanent --add-port=8000/tcp
    firewall-cmd --reload
    echo "✓ Firewalld настроен"
fi

# Создание systemd service
echo ""
echo "Создание systemd сервиса..."
cat > /etc/systemd/system/tenderparser.service << EOF
[Unit]
Description=Tender Parser Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tenderparser
Environment="PATH=/opt/tenderparser/venv/bin"
ExecStart=/opt/tenderparser/venv/bin/python -u /opt/tenderparser/multi_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tenderparser

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✅ УСТАНОВКА ЗАВЕРШЕНА!"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "Для запуска сервера выполните:"
echo "  systemctl start tenderparser"
echo ""
echo "Для просмотра логов:"
echo "  journalctl -u tenderparser -f"
echo ""
echo "Или запустите вручную:"
echo "  cd /opt/tenderparser"
echo "  source venv/bin/activate"
echo "  python -u multi_server.py"
echo ""
echo "Доступ к интерфейсу:"
echo "  http://$(hostname -I | awk '{print $1}'):8000"
echo ""
echo "════════════════════════════════════════════════════════════"
echo ""
echo "⚠️  ВНИМАНИЕ: Настройте безопасность!"
echo "    См. файл УСТАНОВКА_НА_СЕРВЕР.md"
echo ""

