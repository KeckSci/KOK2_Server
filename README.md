需要在电脑上建一个210.209.17.129的虚拟网卡，用windows自带的Microsoft Loopback Adapter：


第一步：添加 Windows 自带虚拟网卡

按：

Win + R

输入：

hdwwiz

回车。

然后按这个流程：

下一步→ 安装我手动从列表选择的硬件→ 网络适配器→ Microsoft→ Microsoft KM-TEST Loopback Adapter→ 下一步安装

安装完成后，你会多一个虚拟网卡。


第二步：给虚拟网卡改名

打开 PowerShell：

Get-NetAdapter

你会看到类似：

Ethernet

WLAN

Ethernet 2

找到那个新出现的 Loopback 网卡，比如叫Ethernet 2，可以改名：

Rename-NetAdapter -Name "Ethernet 2" -NewName "KOK2Loopback"

如果名字不同，把"Ethernet 2"换成实际名字。


第三步：给 Loopback 网卡添加旧服务器 IP

管理员 PowerShell：

New-NetIPAddress -InterfaceAlias "KOK2Loopback" -IPAddress 210.209.17.129 -PrefixLength 32

然后确认：

ipconfig

应该能看到KOK2Loopback上有：

210.209.17.129
