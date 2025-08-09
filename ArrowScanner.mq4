#property strict
#property version   "1.01"

#include <lib_websockets.mqh>

// === НАСТРОЙКИ ===
input string Host = "ws://localhost:8080";
input int    HeatBeatPeriod = 45;
input int    commandPingMilliseconds = 1000;

// === ВЫПАДАЮЩЕЕ МЕНЮ ИНДИКАТОРОВ ===
enum IndicatorType
{
   ConnorsRSI,        // импульсconnorsrsi_arrow-A
   SuperArrows        // super_arrows (пример)
};
input IndicatorType SelectedIndicator = ConnorsRSI;

// === ПЕРЕМЕННЫЕ ===
WebSocketsProcessor *ws;
datetime lastBarTime = 0;

string IndicatorName = "";
int BufferUp = 0;
int BufferDown = 1;
int brokerOffsetHours;
int moscowOffsetHours;

// === ИНИЦИАЛИЗАЦИЯ ===
int OnInit()
{
   ConfigureIndicator();
   brokerOffsetHours = (int)((TimeCurrent() - TimeGMT()) / 3600);
   moscowOffsetHours = 3 - brokerOffsetHours;
   

   ws = new WebSocketsProcessor(Host, 30, HeatBeatPeriod);
   ws.SetHeader("account", AccountNumber());
   ws.Init();
   
   lastBarTime = iTime(Symbol(), 0, 0);

   EventSetMillisecondTimer(commandPingMilliseconds);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   delete ws;
}

void OnTimer()
{
   // --- проверка новой свечи ---
   datetime currentBarTime = iTime(Symbol(), 0, 0);
   if (currentBarTime != lastBarTime)
   {
      lastBarTime = currentBarTime;
      CheckArrowOnLastClosedBar();
   }

   string cmd = ws.GetCommand();
   if (cmd != "")
      Print("Получена команда: ", cmd);
}

// === НАСТРОЙКА ИНДИКАТОРА ПО ВЫБОРУ ПОЛЬЗОВАТЕЛЯ ===
void ConfigureIndicator()
{
   switch (SelectedIndicator)
   {
      case ConnorsRSI:
         IndicatorName = "импульсconnorsrsi_arrow-A";
         BufferUp      = 0;
         BufferDown    = 1;
         break;

      case SuperArrows:
         IndicatorName = "super_arrows";
         BufferUp      = 1;
         BufferDown    = 2;
         break;

      default:
         IndicatorName = "";
         BufferUp      = 0;
         BufferDown    = 1;
   }
}

// === ПРОВЕРКА СТРЕЛОК НА ЗАКРЫТОЙ СВЕЧЕ ===
void CheckArrowOnLastClosedBar()
{
   int shift = 1;
   double upArrow   = iCustom(NULL, 0, IndicatorName, BufferUp, shift);
   double downArrow = iCustom(NULL, 0, IndicatorName, BufferDown, shift);

   string tf = GetPeriodString();
   datetime barTime = iTime(NULL, 0, shift);

   datetime barTimeMoscow = barTime + moscowOffsetHours * 3600;

   // --- Формат ISO для Python ---
   string barTimeStr = TimeToString(barTimeMoscow, TIME_DATE | TIME_SECONDS);
   StringReplace(barTimeStr, ".", "-"); // 2025-08-08 вместо 2025.08.08

   // --- Определяем код направления ---
   int directionCode = 0; // 0 = нет сигнала
   if (upArrow != EMPTY_VALUE && downArrow == EMPTY_VALUE)
      directionCode = 1; // вверх
   else if (downArrow != EMPTY_VALUE && upArrow == EMPTY_VALUE)
      directionCode = 2; // вниз
   else if (upArrow != EMPTY_VALUE && downArrow != EMPTY_VALUE)
      directionCode = 3; // оба

   // --- Формируем JSON ---
   string payload;
   payload = "{";
   payload += "\"symbol\":\"" + Symbol() + "\",";
   payload += "\"direction\":" + IntegerToString(directionCode) + ",";
   payload += "\"timeframe\":\"" + tf + "\",";
   payload += "\"datetime\":\"" + barTimeStr + "\"";
   payload += "}";

   ws.Send(payload);
   Print("Отправлено по WebSocket: ", payload);
}

// === ЧЕЛОВЕКОЧИТАЕМОЕ НАЗВАНИЕ ТАЙМФРЕЙМА ===
string GetPeriodString()
{
   switch (Period())
   {
      case PERIOD_M1:   return "M1";
      case PERIOD_M5:   return "M5";
      case PERIOD_M15:  return "M15";
      case PERIOD_M30:  return "M30";
      case PERIOD_H1:   return "H1";
      case PERIOD_H4:   return "H4";
      case PERIOD_D1:   return "D1";
      case PERIOD_W1:   return "W1";
      case PERIOD_MN1:  return "MN1";
      default:          return "Unknown";
   }
}

