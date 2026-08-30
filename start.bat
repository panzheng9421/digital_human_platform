@echo off
chcp 65001 >nul
cd /d D:\ai\workbuddy\opc\digital_human_platform
call venv\Scripts\activate

REM ===== 安全密钥（JWT 签名，必须设置；本地开发用固定值即可）=====
set DH_SECRET_KEY=f7e07d681a91827f025b8ad056746c763c51252ec265e1894b016c98a28b0e65

REM ===== 配音（声音克隆）：CosyVoice2 on PAI-EAS（替代 fish+asr）=====
REM 1=启用真实声音克隆；0=占位音频。端点与 token 来自 PAI-EAS 部署的 CosyVoice2 服务
set COSYVOICE_ENABLED=1
set COSYVOICE_ENDPOINT=http://cosyvoice001.1511087800506388.cn-hangzhou.pai-eas.aliyuncs.com
set COSYVOICE_TOKEN=OGRkYjA4N2ZkZjBkZGFiZmNkYzViYjJlZmQ1ZDMyMTkzODYwOTMwOA==
set COSYVOICE_FORMAT=wav
REM 合成模式固定在 cosyvoice_client.SYNTHESIS_MODE（自然语言复刻），此处无需配置

REM ===== 数字人推理服务配置（EAS 部署的 duix.avatar / HeyGem）=====
set AVATAR_PROVIDER=heygem
set HEYGEM_ENDPOINT=http://1511087800506388.cn-hangzhou.pai-eas.aliyuncs.com/api/predict/eas001
set HEYGEM_TOKEN=YmYwOTIwODE1ZDliMzU4MzliMjg2Njk5NTNiYTI1OWFhZDdkNjIyOA==
REM duix.avatar 容器内接口前缀（/easy/submit、/easy/query）
set HEYGEM_PATH_PREFIX=easy

REM ===== OSS 中转配置（本地平台 <-> 云端 EAS 的文件桥）=====
REM 把下面 4 项填成你自己的阿里云 OSS 信息（与 EAS 容器里填的保持一致）
set OSS_BUCKET=oss-pai-b3nriy9npftmrt6q9e-cn-hangzhou
set OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
REM EAS 容器内无公网出口，给 EAS 的输入 URL 用内网 endpoint（本地平台上传仍走公网）
set OSS_ENDPOINT_INTERNAL=oss-cn-hangzhou-internal.aliyuncs.com
set OSS_ACCESS_KEY_ID=LTAI5tANFfPVrSsc5G4sP1gr
set OSS_ACCESS_KEY_SECRET=BB0GaYcX96AojmWbqFjen6TcqnNf1O
set OSS_REGION=cn-hangzhou
REM 0=返回签名URL(更安全,默认)  1=Bucket公共读返回公网直链
set OSS_PUBLIC_READ=0

echo ============================================
echo  数字人短视频智能体平台 启动中...
echo  本机访问: http://localhost:8000
echo  默认账号: laopan / laopan123
echo ============================================
python main.py
pause
