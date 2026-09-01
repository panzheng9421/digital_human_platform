网感大字 / 字幕 的中文字体兜底目录
====================================

Linux（ECS）默认没有中文字体，ffmpeg/libass 烧录字幕会出方块字。
本项目 media_utils._resolve_cjk_font() 按以下顺序找中文字体：

1. 本目录（app/assets/fonts/）：往这里丢任意一个 .ttf / .ttc / .otf 中文字体，
   自动生效，无需 root、无需 apt。推荐：wqy-microhei.ttc 或 NotoSansCJK。
2. 系统已装字体（Windows 微软雅黑 / Linux 常见中文字体绝对路径）。
3. fc-list 找到的任意中文家族名（交给 fontconfig 解析）。

兜底行为：以上都找不到时，自动跳过「网感大字」，绝不烧录出方块字，不崩溃。

一键安装（需要 root 时才用）：
  Ubuntu/Debian:  apt-get update && apt-get install -y fonts-wqy-zenhei
  CentOS/RHEL:    yum install -y wqy-zenhei-fonts
安装后 libass 即可通过 fontconfig 找到中文字体，无需放本目录。
