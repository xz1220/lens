# lens 日常入口（等价于 python3 lens.py <子命令>，习惯哪个用哪个）
.PHONY: morning demo serve test check

morning:        ## 晨间一条龙：采集 → AI 总结 → 看板
	python3 lens.py morning

demo:           ## 示例数据看板（不采集、不跑 LLM，克隆即看）
	python3 server.py --demo --open

serve:          ## 只起看板
	python3 server.py --open

test:           ## 全套零网络测试
	python3 -m unittest discover -s tests -p 'test_*.py'

check: test     ## 测试 + 隐私闸：data/ 下不得有被 git 跟踪的文件
	@test -z "$$(git ls-files data)" || { echo 'data/ 下有文件被 git 跟踪！私人数据不得进仓库'; exit 1; }
	@echo "check ok：测试全过，data/ 干净"
