@echo off
chcp 65001 >nul
rem 演示实例（D8/D16）：业务库只读复制成演示副本 data_demo\，服务器时钟定在演示日 2026-09-22，调度关闭；业务库不动。
rem 副本含原始照片（media\），只在本机使用、不外传，录完即可删除整个 data_demo\。
rem 用法：start-demo.bat           用已有副本启动（还没有副本时先制作），离线模拟模型（mock）
rem       start-demo.bat copy      重新制作副本（覆盖旧副本）后启动
rem       start-demo.bat real-ai   临时切到真实交融模型：须用户当次授权；Key 从 .env 导入，退出后删除副本里的 Key 文件
rem 其他参数（端口、手机访问等）直接用：.venv\Scripts\python -m tools.demo_serve --help
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo 请先运行 start.bat 创建虚拟环境
  goto :err
)
.venv\Scripts\python -c "import time_machine" 2>nul || (
  echo 缺少演示依赖 time-machine，请先运行：.venv\Scripts\python -m pip install -r requirements-demo.txt
  goto :err
)
set AI=
if /i "%~1"=="real-ai" set AI=--real-ai
if /i "%~1"=="copy" goto :copy
if exist data_demo\DEMO_COPY.json goto :serve
:copy
.venv\Scripts\python -m tools.demo_copy --force || goto :err
:serve
if defined AI (
  echo.
  echo ************************************************************************
  echo **  真实 AI 模式：将调用交融大模型，消耗额度，演示数据会发往交融。
  echo **  只在用户当次授权录制“AI 现场产出”时使用；录完按 Ctrl+C 退出。
  echo ************************************************************************
  echo.
)
.venv\Scripts\python -m tools.demo_serve %AI%
rem 无论怎样退出，都再删一次副本里的 Key 池文件（真实 AI 模式会生成）
del /q data_demo\ai_keys.json data_demo\ai_keys.tmp 2>nul
exit /b
:err
pause
exit /b 2
