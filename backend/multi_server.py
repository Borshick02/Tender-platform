#!/usr/bin/env python3
"""
HTTP сервер для мультипарсера тендеров (RTS-Tender + RUTEND)
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import socket
import threading
import time
from pathlib import Path
import sys
import io

if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Проверка доступности парсеров
try:
    from multi_parser import run_multi_search, MultiSearchConfig
    MULTI_PARSER_AVAILABLE = True
except ImportError:
    MULTI_PARSER_AVAILABLE = False
    print("⚠️ Multi-parser недоступен! Используется режим демонстрации.")

# Базовая папка проекта (где лежат HTML-файлы)
BASE_DIR = Path(__file__).parent


class ExclusiveHTTPServer(HTTPServer):
    """
    На Windows предотвращает запуск нескольких серверов на одном порту.
    Иначе иногда получается несколько LISTEN на :8000, и браузер попадает "не в тот" процесс.
    """

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()

# Глобальное хранилище задач
tasks = {}
task_counter = 0


class TeeOutput:
    """Класс для дублирования вывода в терминал и в задачу"""
    def __init__(self, task, original_stream):
        self.task = task
        self.original_stream = original_stream
    
    def write(self, text):
        # Записываем в оригинальный поток
        self.original_stream.write(text)
        self.original_stream.flush()
        
        # Добавляем в логи задачи
        if text.strip():  # Игнорируем пустые строки
            self.task.logs.append(text.rstrip())
            # Ограничиваем количество логов (последние 1000 строк)
            if len(self.task.logs) > 1000:
                self.task.logs = self.task.logs[-1000:]
    
    def flush(self):
        self.original_stream.flush()
    
    def __getattr__(self, name):
        return getattr(self.original_stream, name)


class ParserTask:
    def __init__(self, task_id, query, pages, sources,
                 headless=True,
                 globalsources_use_chrome_profile=False,
                 chrome_profile_dir="Default",
                 chrome_user_data_dir=None,
                 globalsources_mode="auto",
                 rts_law_44fz=True, rts_law_223fz=True, rts_law_615pp=True,
                 rts_law_small_volume=True, rts_law_commercial=True, 
                 rts_law_commercial_offers=True,
                 china_match=False, china_pages=1, china_top_k=5, china_max_queries=6, china_min_score=0.05,
                 china_translate_en=True,
                 china_max_tenders=50,
                 china_queries_per_tender=6,
                 china_match_scope="all_positions",
                 china_proxy_list=None):
        self.id = task_id
        self.query = query
        self.pages = pages
        self.sources = sources if sources else [
            'rts',
            'rutend',
            'synapse',
            'tenderpro',
            'rostender',
            'tektorg',
            'b2b',
            'sberbank_ast',
            'zakazrf',
            'roseltorg',
            'fabrikant',
            'etpgpb',
            'goszakup_kz',
            'china_1688',
            'made_in_china',
            'dhgate',
            'globalsources',
            'hktdc_sourcing',
            'b2bchinasources',
            'b2bmap',
        ]
        self.headless = bool(headless)
        self.globalsources_use_chrome_profile = bool(globalsources_use_chrome_profile)
        self.chrome_profile_dir = chrome_profile_dir or "Default"
        self.chrome_user_data_dir = chrome_user_data_dir
        self.globalsources_mode = globalsources_mode if globalsources_mode in ("auto", "http", "playwright") else "auto"
        self.status = "running"  # running, completed, error
        self.progress = 0
        self.results = []
        self.error = None
        self.logs = []  # Логи выполнения
        self.start_time = time.time()
        self.end_time = None
        # Фильтры RTS-Tender
        self.rts_law_44fz = rts_law_44fz
        self.rts_law_223fz = rts_law_223fz
        self.rts_law_615pp = rts_law_615pp
        self.rts_law_small_volume = rts_law_small_volume
        self.rts_law_commercial = rts_law_commercial
        self.rts_law_commercial_offers = rts_law_commercial_offers
        # Подбор предложений с китайских площадок под каждый тендер
        self.china_match = china_match
        self.china_pages = china_pages
        self.china_top_k = china_top_k
        self.china_max_queries = china_max_queries
        self.china_min_score = china_min_score
        self.china_translate_en = china_translate_en
        self.china_max_tenders = china_max_tenders
        self.china_queries_per_tender = china_queries_per_tender
        self.china_match_scope = china_match_scope
        self.china_proxy_list = china_proxy_list or []


def run_parser(task):
    # Перехватываем stdout и stderr для дублирования в логи
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    
    tee_stdout = TeeOutput(task, original_stdout)
    tee_stderr = TeeOutput(task, original_stderr)
    
    try:
        # Перенаправляем вывод
        sys.stdout = tee_stdout
        sys.stderr = tee_stderr
        
        if not MULTI_PARSER_AVAILABLE:
            for i in range(10):
                time.sleep(0.5)
                task.progress = int((i + 1) / 10 * 100)
                print(f"Прогресс: {task.progress}%")
            
            # Генерируем фейковые результаты для каждого источника
            print("Генерация демо-результатов...")
            demo_results = []
            
            if 'rts' in task.sources:
                for i in range(3):
                    demo_results.append({
                        "title": f"Тендер RTS {i+1}: {task.query}",
                        "url": f"https://www.rts-tender.ru/tender/{i+1}",
                        "source": "RTS-TENDER",
                        "customer": f"ООО «Заказчик {i+1}»",
                        "organizer": f"ООО «Организатор {i+1}»",
                        "price": f"{(i+1) * 150000} ₽",
                        "status": "Прием заявок",
                        "law_type": "223-ФЗ",
                        "purchase_type": "АУКЦИОН",
                    })
            
            if 'rutend' in task.sources:
                for i in range(5):
                    demo_results.append({
                        "title": f"Тендер RUTEND {i+1}: {task.query}",
                        "url": f"https://rutend.ru/tender/{i+1}",
                        "source": "RUTEND",
                        "price": f"{(i+1) * 200000} ₽",
                        "law_type": "44-ФЗ",
                        "purchase_type": "Электронный аукцион",
                        "deadline": "25.12.2025",
                    })
            
            if 'synapse' in task.sources:
                for i in range(7):
                    demo_results.append({
                        "title": f"Тендер Synapse {i+1}: {task.query}",
                        "url": f"https://synapsenet.ru/tender/{i+1}",
                        "source": "SYNAPSE",
                        "price": f"{(i+1) * 175000} ₽",
                        "law_type": "223-ФЗ",
                        "purchase_type": "Конкурс",
                        "deadline": "20.12.2025",
                        "platform": "ЭТП Система",
                    })
            
            if 'tenderpro' in task.sources:
                for i in range(6):
                    demo_results.append({
                        "title": f"Тендер Tender.Pro {i+1}: {task.query}",
                        "url": f"https://www.tender.pro/tender/{i+1}",
                        "source": "TENDER.PRO",
                        "customer": f"ООО «Компания {i+1}»",
                        "status": "Открыт",
                        "tender_id": f"112{7410+i}",
                        "deadline": "31.12.2025",
                    })
            
            if 'rostender' in task.sources:
                for i in range(8):
                    demo_results.append({
                        "title": f"РосТендер {i+1}: {task.query}",
                        "url": f"https://rostender.info/tender/{i+1}",
                        "source": "ROSTENDER",
                        "customer": f"Заказчик {i+1}",
                        "price": f"{(i+1) * 200000} ₽",
                        "law_type": "44-ФЗ" if i % 2 == 0 else "223-ФЗ",
                        "region": "Москва" if i % 3 == 0 else "Санкт-Петербург",
                        "deadline": "28.12.2025",
                        "platform": "ЕИС",
                    })
            
            if 'tektorg' in task.sources:
                for i in range(6):
                    demo_results.append({
                        "title": f"ТЭК-Торг процедура {i+1}: {task.query}",
                        "url": f"https://www.tektorg.ru/procedure/{i+1}",
                        "source": "TEKTORG",
                        "tender_id": f"32202405{500+i}",
                        "organizer": f"ПАО «ТЭК {i+1}»",
                        "price": f"{(i+1) * 250000} ₽",
                        "law_type": "223-ФЗ" if i % 3 != 0 else "615-ПП",
                        "purchase_type": "Запрос предложений" if i % 2 == 0 else "Электронный аукцион",
                        "region": "Москва" if i % 2 == 0 else "Санкт-Петербург",
                        "deadline": "28.12.2025 15:00",
                        "status": "Прием заявок",
                        "platform": "ТЭК-Торг",
                    })
            if 'zakazrf' in task.sources:
                for i in range(4):
                    demo_results.append({
                        "title": f"ЗаказРФ процедура {i+1}: {task.query}",
                        "url": f"https://www.zakazrf.ru/NotificationEx/View/{i+1}",
                        "source": "ZAKAZRF",
                        "tender_id": f"0375{i+1}0000122600001{i}",
                        "organizer": f"ГБУ «Заказчик {i+1}»",
                        "customer": f"Заказчик {i+1}",
                        "purchase_type": "Электронный аукцион" if i % 2 == 0 else "Запрос котировок",
                        "deadline": "28.02.2026",
                        "publish_date": "08.02.2026",
                    })
            if 'roseltorg' in task.sources:
                for i in range(4):
                    demo_results.append({
                        "title": f"Росэлторг процедура {i+1}: {task.query}",
                        "url": f"https://www.roseltorg.ru/procedure/SP1005477{i}",
                        "source": "ROSELTORG",
                        "tender_id": f"SP1005477{i}",
                        "organizer": f"ГБПОУ «Колледж {i+1}»",
                        "region": "77. г. Москва",
                        "deadline": "28.02.2026",
                    })
            if 'fabrikant' in task.sources:
                for i in range(4):
                    demo_results.append({
                        "title": f"Фабрикант процедура {i+1}: {task.query}",
                        "url": f"https://www.fabrikant.ru/procedure/100597{i}",
                        "source": "FABRIKANT",
                        "tender_id": f"100597{i}",
                        "organizer": f"СПБ ГБУЗ «Центр {i+1}»",
                        "purchase_type": "Электронный аукцион",
                        "publish_date": "06.02.2026 18:51",
                        "deadline": "16.02.2026 09:00",
                    })
            if 'etpgpb' in task.sources:
                for i in range(4):
                    demo_results.append({
                        "title": f"Перчатки медицинские {i+1}",
                        "url": f"https://etp.gpb.ru/#com/procedure/view/procedure/121296{i}",
                        "source": "ETPGPB",
                        "tender_id": f"3261566311{i}",
                        "organizer": f"ГАУ РК ДЖАНКОЙСКАЯ ГОРОДСКАЯ ПОЛИКЛИНИКА",
                        "purchase_type": "Запрос котировок в электронной форме для СМСП",
                        "price": "461700.0 RUB",
                        "publish_date": "02.02.2026 15:40",
                        "deadline": "10.02.2026 09:00",
                        "region": "Республика Крым",
                    })
            
            task.results = demo_results
            print(f"✓ Собрано {len(task.results)} демо-результатов")
            task.status = "completed"
            task.end_time = time.time()
            return
        
        # Реальный парсинг
        print(f"Инициализация мультипарсера для запроса: '{task.query}'")
        
        cfg = MultiSearchConfig(
        query=task.query,
        pages=task.pages,
        output=Path(f"results_{task.id}.json"),
        headless=task.headless,
        sources=task.sources,
        parallel=False,
        rts_law_44fz=task.rts_law_44fz,
        rts_law_223fz=task.rts_law_223fz,
        rts_law_615pp=task.rts_law_615pp,
        rts_law_small_volume=task.rts_law_small_volume,
        rts_law_commercial=task.rts_law_commercial,
        rts_law_commercial_offers=task.rts_law_commercial_offers,
        china_match=task.china_match,
        china_match_pages=task.china_pages,
        china_match_top_k=task.china_top_k,
        china_match_max_queries=task.china_max_queries,
        china_match_min_score=task.china_min_score,
        china_translate_en=task.china_translate_en,
        china_match_max_tenders=task.china_max_tenders,
        china_match_queries_per_tender=task.china_queries_per_tender,
        china_match_scope=task.china_match_scope,
        globalsources_use_chrome_profile=task.globalsources_use_chrome_profile,
        chrome_profile_dir=task.chrome_profile_dir,
        chrome_user_data_dir=task.chrome_user_data_dir,
        globalsources_mode=task.globalsources_mode,
        china_proxy_list=task.china_proxy_list if task.china_proxy_list else None,
        )
        
        print(f"Конфигурация: {task.pages} страниц, источники: {', '.join(task.sources)}")
        
        # Обновляем прогресс во время работы
        def update_progress():
            for p in range(0, 90, 5):
                if task.status != "running":
                    break
                task.progress = p
                time.sleep(3)
        
        progress_thread = threading.Thread(target=update_progress, daemon=True)
        progress_thread.start()
        
        # Запускаем парсинг
        print("Запуск мультипарсинга...")
        results = run_multi_search(cfg)
        
        # Объединяем результаты
        print(f"Обработка результатов...")
        task.results = results.get('combined', [])
        
        print(f"✓ Парсинг завершен успешно. Собрано {len(task.results)} результатов")
        print(f"  - RTS-TENDER: {len(results.get('rts', []))} тендеров")
        print(f"  - RUTEND: {len(results.get('rutend', []))} тендеров")
        
        task.status = "completed"
        task.progress = 100
        task.end_time = time.time()
        
    except Exception as e:
        import traceback
        error_msg = f"Ошибка парсинга: {e}"
        print(error_msg)
        print(traceback.format_exc())
        task.status = "error"
        task.error = str(e)
        task.end_time = time.time()
    finally:
        # Восстанавливаем оригинальные потоки
        sys.stdout = original_stdout
        sys.stderr = original_stderr


class RequestHandler(BaseHTTPRequestHandler):
    
    def _send_cors_headers(self):
        """Добавляем CORS заголовки"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    
    def _send_json(self, data, status=200):
        """Отправка JSON ответа"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _send_html(self, file_path):
        """Отправка HTML файла"""
        try:
            path = Path(file_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            content = path.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
        except FileNotFoundError:
            self.send_error(404, "File not found")
    
    def do_OPTIONS(self):
        """Обработка OPTIONS запросов для CORS"""
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()
    
    def do_GET(self):
        """Обработка GET запросов"""
        global task_counter
        
        # Главная страница
        if self.path == '/' or self.path == '/index.html':
            self._send_html('multi_search.html')
            return
        
        # Старая версия (только RTS)
        if self.path == '/rts.html':
            self._send_html('index.html')
            return
        
        # Статус задачи
        if self.path.startswith('/status/'):
            task_id = self.path.split('/')[-1]
            if task_id in tasks:
                task = tasks[task_id]
                duration = None
                if task.end_time:
                    duration = round(task.end_time - task.start_time, 1)
                
                self._send_json({
                    'id': task.id,
                    'status': task.status,
                    'progress': task.progress,
                    'query': task.query,
                    'pages': task.pages,
                    'sources': task.sources,
                    'total_results': len(task.results),
                    'results': task.results if task.status == 'completed' else [],
                    'error': task.error,
                    'duration': duration,
                    'logs': task.logs  # Добавляем логи
                })
            else:
                self._send_json({'error': 'Task not found'}, 404)
            return
        
        self.send_error(404)
    
    def do_POST(self):
        """Обработка POST запросов"""
        global task_counter, tasks
        
        if self.path == '/search':
            # Читаем данные
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                query = data.get('query', '').strip()
                pages = int(data.get('pages', 1))
                sources = data.get('sources', ['rts', 'rutend'])
                headless = bool(data.get('headless', True))
                globalsources_use_chrome_profile = bool(data.get('globalsources_use_chrome_profile', False))
                chrome_profile_dir = (data.get('chrome_profile_dir') or 'Default')
                chrome_user_data_dir = data.get('chrome_user_data_dir', None)
                globalsources_mode = data.get('globalsources_mode', 'auto')
                
                # Фильтры RTS-Tender
                rts_law_44fz = data.get('rts_law_44fz', True)
                rts_law_223fz = data.get('rts_law_223fz', True)
                rts_law_615pp = data.get('rts_law_615pp', True)
                rts_law_small_volume = data.get('rts_law_small_volume', True)
                rts_law_commercial = data.get('rts_law_commercial', True)
                rts_law_commercial_offers = data.get('rts_law_commercial_offers', True)

                # Подбор китайских предложений
                china_match = bool(data.get('china_match', False))
                china_pages = int(data.get('china_pages', 1))
                china_top_k = int(data.get('china_top_k', 5))
                china_max_queries = int(data.get('china_max_queries', 6))
                china_min_score = float(data.get('china_min_score', 0.05))
                china_translate_en = bool(data.get('china_translate_en', True))
                china_max_tenders = int(data.get('china_max_tenders', 50))
                china_queries_per_tender = int(data.get('china_queries_per_tender', 6))
                china_match_scope = data.get('china_match_scope', 'all_positions')
                if china_match_scope not in ('all_positions', 'key_only'):
                    china_match_scope = 'all_positions'

                china_proxy_list_raw = data.get('china_proxy_list', [])
                if isinstance(china_proxy_list_raw, str):
                    china_proxy_list = [line.strip() for line in china_proxy_list_raw.splitlines() if line.strip()]
                elif isinstance(china_proxy_list_raw, list):
                    china_proxy_list = [str(p).strip() for p in china_proxy_list_raw if str(p).strip()]
                else:
                    china_proxy_list = []
                
                if not query:
                    self._send_json({'error': 'Query is required'}, 400)
                    return
                
                if pages < 1 or pages > 10:
                    self._send_json({'error': 'Pages must be between 1 and 10'}, 400)
                    return
                
                if not sources or len(sources) == 0:
                    self._send_json({'error': 'At least one source is required'}, 400)
                    return
                
                # Валидация источников
                valid_sources = [
                    'rts',
                    'rutend',
                    'synapse',
                    'tenderpro',
                    'rostender',
                    'tektorg',
                    'b2b',
                    'sberbank_ast',
                    'zakazrf',
                    'roseltorg',
                    'fabrikant',
                    'etpgpb',
                    'goszakup_kz',
                    'china_1688',
                    'made_in_china',
                    'dhgate',
                    'globalsources',
                    'hktdc_sourcing',
                    'b2bchinasources',
                    'b2bmap',
                ]
                sources = [s for s in sources if s in valid_sources]
                
                if len(sources) == 0:
                    self._send_json({'error': 'No valid sources specified'}, 400)
                    return
                
                # Создаем задачу
                task_counter += 1
                task_id = f"task_{task_counter}"
                task = ParserTask(task_id, query, pages, sources,
                                headless,
                                globalsources_use_chrome_profile, chrome_profile_dir, chrome_user_data_dir, globalsources_mode,
                                rts_law_44fz, rts_law_223fz, rts_law_615pp,
                                rts_law_small_volume, rts_law_commercial, 
                                rts_law_commercial_offers,
                                china_match, china_pages, china_top_k, china_max_queries, china_min_score,
                                china_translate_en,
                                china_max_tenders,
                                china_queries_per_tender,
                                china_match_scope,
                                china_proxy_list)
                tasks[task_id] = task
                
                # Запускаем парсинг в отдельном потоке
                thread = threading.Thread(target=run_parser, args=(task,), daemon=True)
                thread.start()
                
                self._send_json({
                    'task_id': task_id,
                    'message': 'Parsing started',
                    'sources': sources
                })
                
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
            return
        
        self.send_error(404)
    
    def log_message(self, format, *args):
        """Логирование запросов"""
        print(f"[{self.log_date_time_string()}] {format % args}")


def run_server(port=8000, host='0.0.0.0'):
    """Запуск сервера"""
    server_address = (host, port)
    httpd = None
    # На Windows после остановки процесса порт может оставаться занятым короткое время (TIME_WAIT),
    # особенно при SO_EXCLUSIVEADDRUSE. Делаем несколько попыток бинда.
    for attempt in range(1, 31):
        try:
            httpd = ExclusiveHTTPServer(server_address, RequestHandler)
            break
        except OSError as e:
            if getattr(e, "winerror", None) == 10048 and sys.platform == "win32":
                if attempt == 1:
                    print(f"⚠️  Порт {port} ещё занят, жду освобождения...")
                time.sleep(0.25)
                continue
            raise
    if httpd is None:
        raise OSError(f"Не удалось занять порт {port}")
    
    print("=" * 80)
    print("🚀 МУЛЬТИПАРСЕР ТЕНДЕРОВ - HTTP СЕРВЕР (6 ПЛОЩАДОК)")
    print("=" * 80)
    print(f"📍 Локально: http://localhost:{port}")
    if host == '0.0.0.0':
        print(f"🌐 Внешний доступ: http://<IP-адрес-сервера>:{port}")
    print(f"🔧 Режим: {'PROD (Multi-Parser)' if MULTI_PARSER_AVAILABLE else 'DEMO (без парсинга)'}")
    print()
    print(f"📌 Поддерживаемые площадки:")
    print(f"   • RTS-Tender (rts-tender.ru)")
    print(f"   • RUTEND (rutend.ru)")
    print(f"   • Synapse (synapsenet.ru) - 200+ площадок")
    print(f"   • Tender.Pro (tender.pro) - ЭТП, 400+ закупок/день")
    print(f"   • РосТендер (rostender.info) - ВСЕ тендеры России")
    print(f"   • ТЭК-Торг (tektorg.ru) - Электронная площадка")
    print("=" * 80)
    print(f"\n✨ Откройте в браузере: http://localhost:{port}")
    print("❌ Для остановки нажмите Ctrl+C\n")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n👋 Сервер остановлен")
        httpd.shutdown()


if __name__ == '__main__':
    port = 8000
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    run_server(port)


