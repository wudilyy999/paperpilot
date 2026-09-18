PY ?= python3

.PHONY: install test test-fast dashboard demo

install:        ## 安装依赖
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e .

test:           ## 全量测试
	$(PY) -m pytest tests/

test-fast:      ## 只跑确定性单测(跳过需要外部 API 的用例)
	PAPERPILOT_OFFLINE_TESTS=1 $(PY) -m pytest tests/ -q

dashboard:      ## 打开前端仪表盘(纯静态,无需后端)
	open frontend/paperpilot_dashboard/index.html

demo:           ## 启动 Streamlit 演示
	cd frontend/demo_light && streamlit run storm.py
