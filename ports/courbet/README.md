# BlueOS для Xiaomi Mi 11 Lite 4G (courbet)

Порт BlueOS на `courbet` (SM7150-AC, Snapdragon 732G) в виде **recovery-zip** для установки через recovery. Собирается автоматически в GitHub Actions: скачивает готовую базу LineageOS 24.0 для courbet и GSI BlueOS, заменяет `system` в базовом OTA на GSI и переподписывает пакет.

Это не сборка BlueOS из исходников и не fastboot-пакет.

## Из чего собирается

| Компонент | Источник |
| --- | --- |
| База (boot, dtbo, vbmeta, vendor, product, system_ext, odm, firmware) | `lineage-24.0-20261106-UNOFFICIAL-courbet.zip` (LineageOS 24.0 / Android 17, дерево [Aciss21](https://github.com/Aciss21)) |
| `system` | BlueOS `v0.8.1-gsi-treble`, файл `BlueOS-0.8.1-gsi_arm64_bvN4-20261107-2.img.gz` из [GDmio/BlueOS](https://github.com/GDmio/BlueOS/releases) |
| Подпись пакета | AOSP public testkey (android_build, ветка `lineage-24.0`) |

Всё устройствозависимое — ядро, вендор, прошивки, разделы — берётся из базы LineageOS. Меняется только системный раздел.

## CI

Workflow: [`.github/workflows/port-courbet.yml`](../../.github/workflows/port-courbet.yml), запуск вручную (`workflow_dispatch`).

Шаги:

1. Скачивает базу по ссылке из Google Drive (`gdown`, при неудаче — прямая ссылка `drive.usercontent.google.com`).
2. Скачивает GSI из GitHub Releases указанного тега и проверяет его по `SHA256SUMS`.
3. Достаёт AOSP testkey из LineageOS `android_build` и конвертирует `testkey.pk8` в PEM.
4. Запускает `ports/courbet/ci/port.py` в режиме `inspect` или `build`.
5. Выкладывает артефакт `BlueOS-courbet-<run>-<mode>` (zip, `SHA256SUMS`, `report.json`) на 14 дней. При заполненном `release_tag` создаёт GitHub Release; пакеты больше 1.9 ГиБ режет на части `.part-*` из-за лимита ассетов 2 ГиБ.

Входные параметры запуска:

- `mode` — `inspect` (только отчёт) или `build` (собрать пакет);
- `base_url`, `base_name` — ссылка и имя базового OTA;
- `gsi_tag`, `gsi_asset` — тег релиза BlueOS и имя GSI-файла;
- `version` — версия BlueOS для имени файла и релиза;
- `release_tag` — необязательный тег GitHub Release;
- `allow_key_mismatch` — разрешить подпись testkey, если база подписана другим ключом (по умолчанию сборка останавливается).

### Что делает `port.py`

Режим `inspect` (ключи и build-окружение не нужны) пишет `report.json`:

- список записей базового zip, наличие `payload.bin`;
- версия и размер `system.transfer.list`, содержимое `dynamic_partitions_op_list`;
- размер системного раздела в базе и в GSI, помещается ли GSI без resize;
- тип ФС GSI (ext4/erofs/sparse), сырой размер, SHA-256;
- сертификат подписи базы (subject).

Режим `build`:

1. Распаковывает GSI (gzip; при необходимости `simg2img`), проверяет, что это `system`-образ.
2. Если GSI больше системного раздела — переписывает `resize system <size>` в `dynamic_partitions_op_list`, предварительно проверив ёмкость группы `qti_dynamic_partitions`.
3. Считает полный список переноса блоков через AOSP `BlockImageDiff` (v4, только команды `new`/`zero`) и собирает `system.new.dat`, сжимает в `system.new.dat.br` (brotli).
4. Пересобирает OTA: заменяет `system.transfer.list`, `system.new.dat.br` и `dynamic_partitions_op_list`, удаляет старые блочные данные `system`.
5. Проверяет размеры (`total_blocks`, размер `new.dat`) и подписывает zip целиком: CMS/PKCS#7 без подписанных атрибутов, SHA-256, в комментарии EOCD — в формате, который понимает verifier LineageOS/Android recovery.
6. Пишет `report.json` и `SHA256SUMS`.

## Установка

1. Разблокированный загрузчик, установленный recovery, совместимый с базой LineageOS.
2. Прошейте свежую прошивку/firmware из базы, если телефон не на ней.
3. В recovery: `Factory reset` / `Format data` (переход с другой прошивки), затем `Apply update` → `Apply from ADB`:

   ```bash
   adb sideload BlueOS-0.8.1-courbet.zip
   ```

4. Перезагрузка в систему; первый запуск BlueOS может занять несколько минут.

Указанная база подписана собственным ключом сборщика, а не AOSP testkey, поэтому для неё в CI нужно `allow_key_mismatch=true`. Приватного ключа базы нет, и порт переподписывается AOSP testkey. При первой установке Lineage recovery может сообщить `Signature verification failed` и предложить `Install anyway`. Если recovery не позволяет продолжить, нужен recovery, который доверяет testkey или разрешает установку после такого предупреждения.

## Ограничения

- Портируется только `system`. Всё остальное — из базы LineageOS 24.0; поведение железа зависит от неё и от GSI.
- GSI-сборка не содержит устройствозависимых HAL/оверлеев BlueOS для courbet, поэтому часть функций может не работать или работать нестабильно. Используется TrebleDroid-вариант GSI: обычный GSI на этом устройстве не загружался (см. `GSI.md` в корне репозитория).
- Нет OTA-обновлений: новая версия — это новый пакет.
- Откат — установка базового `lineage-24.0-...-courbet.zip` (и, при необходимости, format data).
- `--allow-key-mismatch` не делает пакет совместимым с recovery, который не принимает testkey; это только отключает проверку при сборке.

## Структура

```
ports/courbet/
├── README.md
└── ci/
    ├── port.py                 # inspect/build, подпись, отчёты
    └── releasetools/           # AOSP releasetools из LineageOS android_build
        ├── blockimgdiff.py
        ├── rangelib.py
        ├── sparse_img.py
        ├── images.py
        └── common.py
```

## Благодарности

- [GDmio/BlueOS](https://github.com/GDmio/BlueOS) — BlueOS и GSI.
- [LineageOS](https://lineageos.org) — база, AOSP releasetools и testkey.
- [Aciss21](https://github.com/Aciss21) — деревья устройств courbet/sm6150 для Android 17.
- Порт и CI: @qualixxe096.

BlueOS распространяется под лицензией из корня репозитория; сторонние компоненты — под своими лицензиями.
