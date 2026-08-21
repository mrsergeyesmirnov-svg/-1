# Плагин iikoFront: опрос перед закрытием личной смены

Скелет `.cs` на кассу не копируют. Нужна собранная DLL, Manifest.xml и лицензия модуля iiko.

Поведение:

1. Кнопка «Закрыть личную смену».
2. Окно: оценка 1–5 и причина.
3. `POST` в Pulse. Нет `closeShift: true` — смену не закрываем.
4. Есть разрешение — `ClosePersonalSession`. Дальше родное окно iiko с часами.

`employeeId` берётся из текущего пользователя кассы. Список людей в Pulse не нужен.

## Сборка

Visual Studio, .NET Framework 4.7.2, ссылка на `Resto.Front.Api.Vx.dll` той версии, что стоит на кассе. `Copy Local = False`. ModuleId в атрибуте класса = ModuleId в `Manifest.xml` = число от iiko (`api@iiko.ru`), не пример `21000000`.

## Установка на терминал

1. Закрыть iikoFront.
2. Создать папку  
   `C:\Program Files\iiko\iikoRMS\Front.Net\Plugins\PulseShiftSurvey\`
3. Положить туда:
   - `PulseShiftSurveyPlugin.dll`
   - `Manifest.xml`
   - `plugin.json`
4. Запустить iikoFront.

Не класть файлы в корень `Plugins` и не подмешивать в папку чужого плагина.

```json
{
  "pulseUrl": "https://pulse.example.com",
  "apiKey": "тот-же-IIKO_API_KEY",
  "organizationId": "guid-организации-iiko",
  "servicePin": "PIN служебного пользователя iiko с правом закрывать чужие личные смены (F_KIS)"
}
```

Служебный PIN только на кассе. После опроса плагин закрывает смену этого человека от имени служебной учётки.
