#!/bin/sh

chmod 744 /home/pi/qt/out_linux/kill9.sh

#设置库的引用路径
export LD_LIBRARY_PATH=/home/pi/qt/out_linux/lib/mqtt:/home/pi/qt/out_linux/lib/log4cpp:/home/pi/qt/out_linux/lib:$LD_LIBRARY_PATH
#设置显示位置
export DISPLAY=:0.0

/home/pi/qt/out_linux/kill9.sh 

cd  /home/pi/qt/out_linux


#rm -rf /home/pi/qt/out_linux/core*

chmod 777 ./*

#设置生产core不限制大小
ulimit -c unlimited

/home/pi/qt/out_linux/TUI &

#sleep 15

#/home/pi/qt/out_linux/mqttSendUpgradeProgress &

echo "startboot.sh end"

