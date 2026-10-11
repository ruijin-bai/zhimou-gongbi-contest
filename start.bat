@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  echo 首次运行：创建虚拟环境并安装依赖...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
if not exist .env copy .env.example .env >nul && echo 已生成 .env，请填写交融 API Key 后重新运行 && notepad .env && exit /b
if not exist data\zhimou.db (
  set /p PNAME=请输入项目名称：
  .venv\Scripts\python -m app init --project "%PNAME%"
)
echo 启动智眸工笔：http://localhost:8600  （手机与电脑同一网络时，用电脑IP:8600 访问）
.venv\Scripts\python -m app serve
exit /b
:err
echo 安装失败，请确认已安装 Python 3.10+ 并可访问 pip 源
pause
