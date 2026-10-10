# can-motor

Python-драйвер для управления двигателем Gyems через CAN и пример запуска.
Поддерживаются USB-адаптеры UCAN и другие адаптеры с прошивкой CandleLight
через `gs_usb`, а также SocketCAN и SLCAN-адаптеры.

## Установка в Linux

Нужен Python 3.14 или новее. Установить проект можно через `uv` или обычные
`venv` и `pip`. Все команды ниже выполняйте из корня проекта.

Для Ubuntu/Debian установите системные утилиты:

```bash
sudo apt update
sudo apt install can-utils net-tools iproute2
```

`can-utils` предоставляет `slcand`, `net-tools` — `ifconfig`, а `iproute2` — `ip`;
эти утилиты используются при настройке SocketCAN/SLCAN.

### С `uv`

Установите Python-зависимости и пакет проекта:

```bash
uv sync
```

Запускайте `uv sync` от своего пользователя.

### Без `uv`: `venv` и `pip`

Создайте окружение и установите зависимости из `requirements.txt`:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`python3.14` должен быть установлен в системе; можно заменить эту команду
на путь к своему интерпретатору Python 3.14 или новее.
`requirements.txt` содержит версии зависимостей из `uv.lock` и строку `-e .`,
которая устанавливает пакет проекта для импортов `packages.driver` в примерах.
Команды создания окружения и установки пакетов выполняйте от своего пользователя.

### Подсказки в IDE

В IDE выберите интерпретатор
`.venv/bin/python`, чтобы подсказки импортов использовали установленный пакет
`packages.driver` и зависимости проекта.

В VS Code откройте корень проекта и установите расширения Python и Pylance.
Выполните `Python: Select Interpreter` и выберите `.venv/bin/python`.
Настройки проекта включают подсказки автоматического импорта. Если после
установки подсказки не появились, выполните `Developer: Reload Window`.

## Что доустановить для UCAN (`gs_usb`)

Для модуля UCAN с прошивкой CandleLight дополнительно установите системную
библиотеку USB и утилиту для проверки подключения:

```bash
sudo apt install libusb-1.0-0 usbutils
```

`libusb-1.0-0` нужен для доступа к адаптеру через USB. Пакет `usbutils`
предоставляет команду `lsusb` для диагностики.

Зависимость `python-can[gs-usb]` уже указана в `pyproject.toml`.
Python-пакеты `python-can`, `gs-usb` и PyUSB устанавливаются в `.venv`
при любом из способов установки выше. Если окружение было создано до
добавления поддержки UCAN, обновите его через `uv sync` или
`.venv/bin/python -m pip install -r requirements.txt`.
Требования к USB-библиотеке описаны в
[документации python-can](https://python-can.readthedocs.io/en/stable/interfaces/gs_usb.html).

Подключите UCAN по USB и проверьте, что он присутствует в списке устройств:

```bash
lsusb
```

Проверить загрузку Python-зависимостей и `libusb` можно без запуска двигателя:

```bash
sudo .venv/bin/python - <<'PY'
from can.interfaces.gs_usb import GsUsbBus
from usb.backend import libusb1

if libusb1.get_backend() is None:
    raise SystemExit("libusb недоступна: установите libusb-1.0-0")
print("gs_usb, PyUSB и libusb доступны")
PY
```

Этот способ подключения использует `libusb` напрямую; устанавливать или
загружать модуль ядра `gs_usb` для него не требуется. На UCAN должна быть
прошивка CandleLight. Информация о плате и её прошивке есть в
[документации UCAN](https://wiki.fysetc.com/docs/UCAN).

## Запуск с UCAN/CandleLight (`gs_usb`)

Подключите CAN-адаптер с прошивкой CandleLight и двигатель. Отдельный пример
[examples/rotate_motor_gs_usb.py](examples/rotate_motor_gs_usb.py) использует
прямое подключение по USB:

```python
from packages.driver import CanBus

bus = CanBus(channel=0, bustype="gs_usb", bitrate=1_000_000)
```

`channel=0` выбирает первый найденный USB-адаптер. Для нескольких адаптеров
используйте `1`, `2` и далее. Если `channel` не указан при `bustype="gs_usb"`,
выбирается первый адаптер. Скорость `bitrate` задаётся в бит/с и должна
совпадать со скоростью CAN двигателя.

Интерфейс `gs_usb` открывает адаптер напрямую через `libusb`;
для этого подключения не нужны `/dev/ttyACM*`, `slcand`, `ifconfig` или `ip`.

```bash
sudo .venv/bin/python examples/rotate_motor_gs_usb.py
```

Запуск через `sudo` предоставляет доступ к USB-адаптеру. Явный путь
`.venv/bin/python` позволяет использовать зависимости из окружения проекта
при запуске с правами root.

Используйте адаптер только из одной программы одновременно. В Linux
драйвер автоматически отсоединяет активный драйвер ядра от USB-устройства,
поэтому одновременная работа этого адаптера через SocketCAN недоступна.
Подробности есть в [исходном коде gs_usb](https://github.com/jxltom/gs_usb/blob/master/gs_usb/gs_usb.py).

Пример включает двигатель, устанавливает нулевую позицию, задаёт скорость
и выводит его состояние. Для остановки нажмите `Ctrl+C`: пример отправит
команду выключения двигателя и закроет CAN-шину, освободив USB-адаптер.

## SocketCAN и SLCAN

Отдельный пример для этого подключения:
[examples/rotate_motor_socketcan.py](examples/rotate_motor_socketcan.py).

Для старого SLCAN-адаптера оставьте `CanBus()` без аргументов: драйвер ищет
устройство в `/dev/ttyACM*` и создаёт интерфейс `can0` через `slcand`.
Эквивалентная конфигурация с явными параметрами:

```python
bus = CanBus(
    channel="can0",
    bustype="socketcan",
    baudrate=115200,
    bitrate=1_000_000,
)
```

Эта ветка также автоматически ищет последовательный SLCAN-адаптер
и настраивает интерфейс через `slcand`, `ifconfig` и `ip`.
`baudrate` задаёт скорость последовательного порта, а `bitrate` — скорость CAN,
которая должна совпадать со скоростью двигателя. Для CandleLight используйте
`bustype="gs_usb"`, как показано выше.

Запускайте пример с правами root для настройки SLCAN-интерфейса:

```bash
sudo .venv/bin/python examples/rotate_motor_socketcan.py
```

Оба примера выполняют одинаковые команды двигателя и завершаются по `Ctrl+C`.
Исходный `examples/rotate_motor.py` также сохранён и использует `gs_usb`.
