# can-motor

Python-библиотека для стендовых испытаний моторов по **LK-TECH CAN V2.35**.
Пакет `can_motor` разделяет мотор, протокол и CAN-транспорт. Названия API
не зависят от того, как производитель называет свой силовой контроллер.
Совместимость определяется протоколом и прошивкой конкретного устройства.

Два подключения: **SocketCAN** через настроенный интерфейс Linux и
**gs_usb** напрямую к USB-адаптеру с совместимой прошивкой, например CandleLight.
Подробности формата кадров и неоднозначности документации находятся в
[описании протокола](docs/protocol.md), переход со старого API — в
[руководстве по миграции](docs/migration.md).

## Установка

Поддерживаются Python **3.11–3.15** (`>=3.11,<3.16`). Все команды ниже
выполняются из корня репозитория, активация виртуального окружения не требуется.

| ОС | Подключение к мотору | Проверка без оборудования |
|---|---|---|
| Linux | SocketCAN или прямой `gs_usb` | `examples.virtual_motor` |
| Windows | Прямой `gs_usb` с USB-драйвером и libusb | `examples.virtual_motor` |
| macOS | Прямой `gs_usb` с libusb | `examples.virtual_motor` |

Платформы прямого USB-подключения перечислены в
[документации python-can](https://python-can.readthedocs.io/en/v4.6.1/interfaces/gs_usb.html).
Текущая [матрица CI](.github/workflows/tests.yml) проверяет версии Python на Linux;
физическое подключение адаптера под Windows и macOS здесь не проверялось.

### Общий вариант с uv

Если [uv установлен](https://docs.astral.sh/uv/getting-started/installation/),
команды одинаковы в терминале Linux/macOS и PowerShell Windows:

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run python -m examples.virtual_motor
```

В репозитории `.python-version` по умолчанию выбирает Python 3.14. Для другой
версии из поддерживаемого диапазона используйте, например,
`uv sync --python 3.11`, подставив нужную версию.
Для физического USB-подключения дополнительно подготовьте драйвер и системную
библиотеку по разделу своей ОС ниже. `uv sync` устанавливает Python-зависимости,
а системная libusb устанавливается отдельно.

### Linux: системные пакеты и установка через pip

Для Debian/Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip libusb-1.0-0 usbutils iproute2 can-utils
python3 --version
```

`libusb-1.0-0` нужна для прямого `gs_usb`, `usbutils` предоставляет `lsusb`,
`iproute2` — команду `ip`, а `can-utils` — `slcand` для последовательного
SLCAN-адаптера. Для работы только через уже настроенный SocketCAN libusb не нужна.
Если системный Python вне диапазона 3.11–3.15, используйте вариант с `uv`
либо установленный отдельно подходящий интерпретатор.

Установка проекта без `uv`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m examples.virtual_motor
```

После настройки доступа к USB можно читать состояние мотора:

```bash
lsusb
.venv/bin/python -m examples.motor_test --interface gs_usb --channel 0 --node-id 1 --current-scale mg
```

Для SocketCAN сначала выполните [настройку интерфейса Linux](#socketcan-только-linux),
затем запустите:

```bash
.venv/bin/python -m examples.motor_test --interface socketcan --channel can0 --node-id 1 --current-scale mg
```

Python-приложение запускайте от обычного пользователя; `sudo` требуется для
системных команд настройки. Права на USB задаются правилом udev ниже.

### Windows: PowerShell и прямой USB

Установите Python из [официального дистрибутива](https://www.python.org/downloads/windows/).
Ниже выбран Python 3.14; номер можно заменить другой поддерживаемой версией.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m examples.virtual_motor
```

Для физического `gs_usb` нужны два компонента:

1. Драйвер USB-адаптера. Если прошивка уже привязана к WinUSB, используйте его.
   Для установки драйвера можно открыть [Zadig](https://zadig.akeo.ie/), выбрать
   `Options → List All Devices` и свой CAN-адаптер по имени и VID:PID.
   В документации python-can для этого backend указан драйвер **libusbK**.
   Установите драйвер только для выбранного адаптера и переподключите его.
2. Библиотека **libusb 1.0**. Скачайте Windows-архив из
   [официальных выпусков libusb](https://github.com/libusb/libusb/releases)
   и распакуйте `libusb-1.0.dll` подходящей архитектуры, например в
   `C:\tools\libusb`. Архитектура DLL должна совпадать с Python:
   для Python x64 нужна DLL x64.

Добавьте каталог DLL в `PATH` текущего PowerShell и проверьте загрузку:

```powershell
$env:Path = "C:\tools\libusb;$env:Path"
.\.venv\Scripts\python.exe -c "from usb.backend import libusb1; assert libusb1.get_backend() is not None, 'libusb 1.0 not found'; print('libusb OK')"
.\.venv\Scripts\python.exe -m examples.motor_test --interface gs_usb --channel 0 --node-id 1 --current-scale mg
```

Изменение `$env:Path` действует в текущем терминале. Для нового терминала повторите
его или добавьте каталог через настройки переменных среды Windows. Драйвер
адаптера и DLL — отдельные компоненты; детали приведены в
[инструкции libusb для Windows](https://github.com/libusb/libusb/wiki/Windows).

### macOS: Homebrew и прямой USB

При установленном [Homebrew](https://brew.sh/) установите
[Python 3.14](https://formulae.brew.sh/formula/python@3.14) и
[libusb](https://formulae.brew.sh/formula/libusb):

```bash
brew install python@3.14 libusb
python3.14 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m examples.virtual_motor
```

Если Python 3.11–3.15 уже установлен, достаточно `brew install libusb` и создания
окружения нужным интерпретатором. На Apple Silicon используйте Python и libusb
одной архитектуры: например, оба ARM64. Установленная версия PyUSB учитывает
каталог `/opt/homebrew/lib` при поиске библиотеки на ARM64.
См. [поиск библиотек в PyUSB 1.3.1](https://github.com/pyusb/pyusb/blob/v1.3.1/usb/libloader.py).

Проверьте видимость USB-адаптера и загрузку libusb, затем запустите чтение:

```bash
system_profiler SPUSBDataType
.venv/bin/python -c "from usb.backend import libusb1; assert libusb1.get_backend() is not None, 'libusb 1.0 not found'; print('libusb OK')"
.venv/bin/python -m examples.motor_test --interface gs_usb --channel 0 --node-id 1 --current-scale mg
```

Выберите интерпретатор окружения в IDE: `.venv/bin/python` в Linux/macOS или
`.venv\Scripts\python.exe` в Windows. Нативный SocketCAN доступен в Linux.
Если адаптер требует именно SocketCAN/SLCAN, вариант для Windows/macOS — Linux
на отдельном стенде либо виртуальная машина Linux с пробросом USB-адаптера.
Внутри неё выполняются команды раздела Linux; USB должен быть передан этой системе.

## Первый запуск: чтение состояния

Выберите масштаб тока по документации именно вашего мотора. Следующий пример
предполагает подтверждённый профиль MG; он не включает мотор и не задаёт движение:

```python
from can_motor import CanTransport, MG_CURRENT_SCALE, Motor

with CanTransport.gs_usb(channel=0, bitrate=1_000_000) as transport:
    motor = Motor(transport, node_id=1, current_scale=MG_CURRENT_SCALE)
    print(motor.read_health())
    print(motor.read_feedback())
    print(motor.read_angle())
```

Готовый пример по умолчанию также только читает телеметрию. С `uv` прямой USB
запускается одной командой на всех трёх ОС после подготовки libusb и драйвера:

```bash
uv run python -m examples.motor_test --interface gs_usb --channel 0 --node-id 1 --current-scale mg
```

Для Linux с настроенным SocketCAN замените параметры интерфейса на
`--interface socketcan --channel can0`.

`node_id` — номер **1–32**, а не CAN-идентификатор `0x141`. Создание `Motor`
не отправляет кадры. При этом по PDF силовой контроллер после подачи питания
уже включён: локальный запрет движения в Python не подтверждает его физическое
состояние. Начинайте испытание с закреплённым мотором и доступным отключением питания.

## Подключение адаптеров

### SocketCAN (только Linux)

Для обычного CAN-интерфейса Linux настройте скорость, совпадающую с мотором:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 1000000
sudo ip link set can0 up
ip -details -statistics link show can0
```

После этого приложение открывает `CanTransport.socketcan("can0")`.
Библиотека не меняет сетевые настройки. Настройка реального интерфейса и
вариант с `slcand` описаны в
[python-can 4.6.1: SocketCAN](https://python-can.readthedocs.io/en/v4.6.1/interfaces/socketcan.html).

Для старого последовательного SLCAN-адаптера сначала явно выберите устройство:

```bash
ls -l /dev/serial/by-id/
sudo slcand -o -c -s8 -S 115200 /dev/serial/by-id/REPLACE_WITH_YOUR_ADAPTER can0
sudo ip link set can0 up
```

Замените путь на реальный. Здесь `-s8` выбирает CAN 1 Мбит/с,
`-S 115200` — скорость последовательного порта. Для SLCAN скорость задаётся
через `slcand`; повторная команда `ip ... type can bitrate ...` не нужна.
Далее используется тот же `CanTransport.socketcan("can0")`.

### gs_usb / CandleLight

```python
with CanTransport.gs_usb(channel=0, bitrate=1_000_000) as transport:
    motor = Motor(transport, node_id=1, current_scale=MG_CURRENT_SCALE)
    print(motor.read_health())
```

`channel=0` выбирает первый найденный адаптер. При нескольких адаптерах
можно передать `index` либо пару `bus`/`address`; эти способы выбора не смешивают.
USB-адрес может измениться после переподключения. Требуются совместимая прошивка,
PyUSB и системная `libusb`; сетевой интерфейс `can0` этому способу не нужен.
См. [python-can 4.6.1: gs_usb](https://python-can.readthedocs.io/en/v4.6.1/interfaces/gs_usb.html).

В Linux для доступа без `sudo python` проверьте реальный VID:PID командой `lsusb`.
Администратор может установить правило udev только для этого VID:PID, например
для интерактивного локального сеанса:

```text
SUBSYSTEM=="usb", ATTR{idVendor}=="vvvv", ATTR{idProduct}=="pppp", MODE="0660", TAG+="uaccess"
```

Замените `vvvv`/`pppp` значениями устройства, сохраните правило в
`/etc/udev/rules.d/70-can-test.rules`, перезагрузите правила
`sudo udevadm control --reload-rules` и переподключите адаптер.
На стенде без локального сеанса настройте отдельную группу с доступом к этому
устройству. Один адаптер должен принадлежать одному процессу; не используйте его
одновременно через прямой USB-доступ и SocketCAN.

## Ограничения и управление

Ток задаётся в **амперах моментной составляющей тока iq**, скорость — в
**градусах в секунду** или **об/мин (RPM)**, положение — в **градусах**.
Пересчёт скорости: **1 RPM = 6 °/с**. RPM относится к той же механической
координате мотора, что и скорость в протоколе. Для момента в Н·м, тока
питания и координат после редуктора нужны отдельные параметры двигателя;
библиотека не выполняет эти преобразования.

| `MotorLimits` | По умолчанию | Действие |
|---|---:|---|
| `max_current_a` | 1.0 A | Максимальный модуль команды тока и проверяемой телеметрии iq |
| `max_speed_dps` или `max_speed_rpm` | 30 °/с = 5 RPM | Модуль скорости и ограничение скорости позиционирования; при создании выбирают одну единицу |
| `min_position_deg`, `max_position_deg` | −180°, +180° | Диапазон целевых многовитковых положений |
| `max_acceleration_dps2` | 100 °/с² | Верхняя граница настройки ускорения для записи и проверки перед скоростью/позицией |
| `max_temperature_c` | 60 °C | Температурный порог запрета движения |
| `min_voltage_v`, `max_voltage_v` | `None` | Дополнительный контроль напряжения при заданных порогах |
| `firmware_current_limit_a` | `None` | Подтверждённое внешней настройкой ограничение тока для скорости/позиции |

Эти значения — исходные настройки библиотеки, не допустимые параметры любого
мотора. Выберите их по паспорту двигателя, условиям охлаждения и механике стенда.
Превышение лимита, `NaN`, бесконечность и некорректный тип отклоняются до отправки
CAN-команд. Библиотека не подменяет опасный запрос молчаливым ограничением.
Параметры `max_speed_dps` и `max_speed_rpm` взаимоисключающие: передача обоих
в `MotorLimits(...)` вызывает `ValueError`, даже если значения эквивалентны.
В конфигурации скорость хранится как `limits.max_speed_dps`, а свойство
`limits.max_speed_rpm` возвращает тот же предел в RPM и доступно только для чтения.

Пример короткого испытания током:

```python
from time import sleep

from can_motor import CanTransport, MG_CURRENT_SCALE, Motor, MotorLimits

limits = MotorLimits(max_current_a=0.3, max_speed_rpm=2.5)
with CanTransport.gs_usb(channel=0, bitrate=1_000_000) as transport:
    motor = Motor(transport, node_id=1, current_scale=MG_CURRENT_SCALE, limits=limits)
    with motor.control():
        print(motor.set_current(0.2))
        sleep(0.2)
```

Либо запустите пример с явным разрешением команды:

```bash
uv run python -m examples.motor_test --interface gs_usb --channel 0 --node-id 1 --current-scale mg --current-a 0.2 --duration 2
```

В CLI предел скорости задаётся через `--max-speed-rpm 2.5` либо
`--max-speed-dps 15`; эти аргументы также взаимоисключающие. Они настраивают
защитный предел, а команда испытания в этом примере по-прежнему задаётся током.

`enable()` сначала отправляет OFF, чтобы отменить ранее заданное движение,
затем читает состояние защиты и отправляет RUN. **OFF сбрасывает накопленные
обороты и предыдущую команду** по с.5 PDF: после нового включения заново определите
смысл абсолютного многовиткового положения. Включение не сбрасывает ошибки.

`motor.control()` вызывает `enable()` и при выходе пытается отправить OFF,
в том числе после исключения или неудачного включения. `disable()` отправляет
OFF и снимает локальное разрешение движения. `stop()` отправляет STOP и тоже
снимает локальное разрешение; для следующей команды нужен явный `enable()`.
Закрытие самого `CanTransport` освобождает адаптер. Оно не отправляет OFF.

**Команда тока A1 обходит настройку Max Torque Current в фирменной программе**
(PDF, с.7). Поэтому масштаб тока и лимит заданы раздельно:

| Профиль | `full_scale_a`: A1 и ответы на команды движения | `status_full_scale_a`: чтение 9C |
|---|---:|---:|
| `MF_CURRENT_SCALE` | 16.5 A при raw 2048 | 33 A при raw 2048 |
| `MG_CURRENT_SCALE` | 33 A при raw 2048 | 33 A при raw 2048 |

Различие MF взято буквально из противоречивых с.7 и 19 PDF. До испытаний
проверьте обе шкалы на вашей прошивке; при необходимости задайте
`CurrentScale(full_scale_a=..., status_full_scale_a=...)`. Для MH PDF
не определяет масштаб однозначно, поэтому готового профиля MH нет.

Скорость и позиция могут потреблять ток выше значения последней команды A1.
Для этих режимов **заранее настройте и проверьте аппаратный/прошивочный предел тока**,
затем передайте его как `MotorLimits(firmware_current_limit_a=...)`.
Он должен быть не выше `max_current_a`. Это декларация проверенной настройки:
доступный протокол не умеет устанавливать или считывать данный предел.
Без декларации `set_speed()`, `set_speed_rpm()` и `set_position()` запрещены.
Перед ними читается ускорение: настройка должна быть положительной и не выше
`max_acceleration_dps2`.
Если требуется изменение, явно запишите ускорение в RAM при снятом разрешении.

После внешней проверки прошивочного предела конфигурация может выглядеть так:

```python
limits = MotorLimits(
    max_current_a=0.3,
    max_speed_rpm=2.5,
    firmware_current_limit_a=0.3,
)
```

Для `motor`, созданного с этими `limits`, внутри `with motor.control():`
можно явно задать скорость или положение в нужных единицах:

```python
feedback = motor.set_speed_rpm(-1.0)
print(feedback.speed_rpm)
motor.set_position(45, max_speed_rpm=1.5)
```

Знак скорости задаёт направление вращения. `set_speed(speed_dps)` остаётся
доступен; `rpm_to_dps()` и `dps_to_rpm()` из `can_motor` выполняют явный пересчёт.
В `Feedback.speed_dps` сохраняются единицы ответа CAN, а `Feedback.speed_rpm`
вычисляется из них без изменения разрешения измерения.

Позиционирование использует A4 с явной границей скорости:
`motor.set_position(45, max_speed_dps=10)` или
`motor.set_position(45, max_speed_rpm=1.5)`. Одновременно передать оба параметра
нельзя. Если скорость не задана, используется общий предел из `limits`.
Значение переводится в °/с и округляется вниз до целого; допустимый минимум
после преобразования — 1 °/с (1/6 RPM). Пределы положения проверяют
цель A4; они не ограничивают путь в режиме тока или скорости.

Перед каждой командой движения читается свежее состояние защиты. Ответы на
команды движения, а также `read_feedback()` при разрешённом движении проверяются
по току, скорости и температуре. Превышение вызывает попытку OFF и снимает
локальное разрешение. Проверки выполняются только во время вызовов API:
фонового watchdog или гарантированной аварийной остановки здесь нет.

## API для команды

| Метод | Результат / назначение |
|---|---|
| `read_health()` | `Health`: температура, напряжение, биты ошибки |
| `read_feedback()` | `Feedback`: температура, iq, `speed_dps`, вычисляемое `speed_rpm`, отсчёт энкодера |
| `read_angle(single_turn=False)` | Многовитковый угол; `True` — угол в `[0, 360)` |
| `read_encoder()` | `Encoder`: скорректированная, исходная позиции и смещение в отсчётах |
| `read_phase_currents()` | `PhaseCurrents`: температура и три фазных тока |
| `set_current(current_a)` | Команда iq, возвращает `Feedback` |
| `set_speed(speed_dps)` | Команда скорости, возвращает `Feedback` |
| `set_speed_rpm(speed_rpm)` | Команда скорости в об/мин, возвращает `Feedback` |
| `set_position(position_deg, max_speed_dps=...)` / `set_position(position_deg, max_speed_rpm=...)` | Абсолютное многовитковое положение, возвращает `Feedback` |
| `read_pid()` / `write_pid_ram(parameters)` | Шесть коэффициентов `PidParameters` как байты 0–255 |
| `read_acceleration()` / `write_acceleration_ram(acceleration_dps2)` | Ускорение в °/с²; запись только в RAM |
| `clear_errors()` | Явная попытка сброса ошибки с возвратом `Health` |

Изменение PID/ускорения и сброс ошибок выполняются при снятом локальном
разрешении движения и сами сначала отправляют OFF с проверкой подтверждения.
При неудаче OFF запись/сброс не отправляются; эта операция тоже очищает
накопленные обороты. `read_health()` позволяет получить ошибочное состояние
как данные; при разрешённом движении обнаруженная ошибка также вызывает попытку
OFF. Ни включение, ни пример не меняют ноль энкодера в ROM. ROM-записи,
broadcast, управление MS, момент в Н·м и автоматическое разворачивание энкодера
в обороты в API не входят.

Все моторы одной шины должны использовать **один `CanTransport`**.
Транспорт последовательно выполняет запросы, очищает накопленные кадры перед
отправкой и проверяет ID, команду, тип и длину ответа. Не добавляйте параллельный
`recv()`, `Notifier` или второй управляющий процесс для этих же моторов.
Протокол не содержит номера транзакции: сильно задержанный ответ той же команды
может остаться неотличимым от нового. Тайм-аут — повод проверить стенд и заново
явно разрешить движение; автоматических повторов и переподключения нет.

`LimitError` сообщает об ограничении, `SafetyError` — о запрете состояния,
`MotorFaultError` — о неприемлемой телеметрии, `ProtocolError` — о повреждённом
ответе. Ошибки связи доступны как исключения `python-can`, включая
`can.CanTimeoutError` и `can.CanOperationError`. При разрешённом движении ошибка
ввода-вывода/протокола или отказ защиты снимает локальное разрешение и вызывает
попытку OFF. Ошибка чтения в режиме без разрешения движения не добавляет команд
управления. Если USB или CAN уже недоступны, доставка OFF невозможна:
отключение адаптера не гарантирует остановку.

## Проверка на стенде и разработка

1. Проверьте питание, допустимый ток, механику, терминацию CAN и уникальный ID.
   Подключайте мотор при отключённом силовом питании; обеспечьте ручное отключение.
2. Подтвердите протокол, обе шкалы iq и направление вращения по документации
   конкретного мотора. Выберите небольшие лимиты для первого опыта.
3. Запустите пример без `--current-a`. Сопоставьте напряжение, температуру,
   энкодер и угол с реальными показаниями.
4. Выполните короткий тест небольшим током. Проверьте OFF/STOP и повторное
   явное включение. Затем настройте прошивочный предел тока и проверьте
   скорость/позиционирование на свободной безопасной траектории.
5. При снятом питании двигателя проверьте поведение приложения при потере
   CAN/USB. Не проверяйте аварийную остановку единственным отключением USB.

```text
can_motor/models.py          конфигурация и неизменяемая телеметрия с единицами
can_motor/protocol.py        чистое кодирование/декодирование CAN V2.35
can_motor/transport.py       владение шиной и сериализация запросов
can_motor/backends/gs_usb.py USB backend с освобождением исходного устройства
can_motor/motor.py           команды, лимиты и жизненный цикл мотора
examples/motor_test.py       чтение состояния и явный короткий тест током
examples/virtual_motor.py    обмен с эмулятором без оборудования
tests/                      проверки без подключённого мотора
```

Демонстрация без оборудования и дополнительных прав:

```bash
uv run python -m examples.virtual_motor
```

`CanTransport.virtual("test-bus")` открывает внутрипроцессную виртуальную шину;
для ответов нужен второй участник с эмуляцией мотора. Автотесты проверяют
пакеты и защитные сценарии. Они не подтверждают реальную шкалу тока,
электрическую совместимость, динамику или поведение конкретной прошивки.

Версии `python-can==4.6.1` и `gs-usb==0.3.1` закреплены: USB backend использует
их низкоуровневые форматы кадров и переопределяет обработку тайм-аутов и закрытие.
При обновлении зависимостей проверьте эти участки, автотесты и оба адаптера на стенде.
Зависимости задаются в `pyproject.toml`, точные версии хранятся в `uv.lock`.
После изменения зависимостей обновите lock-файл командой `uv lock`.
