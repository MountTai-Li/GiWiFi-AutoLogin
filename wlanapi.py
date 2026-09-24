# -*- coding: utf-8 -*-
"""
wlanapi.py —— 直接调用 Windows 原生 WLAN API（wlanapi.dll）

为什么不用 `netsh wlan show networks`：
    笔记本在已经连上 AP 的情况下，Windows 只会做「定向扫描」（只找漫游候选），
    netsh 于是常常只返回**当前连接的那一个**网络 —— 实测本机就是 1 个，
    而真实环境里有 7 个。所以扫描必须由我们自己用 WlanScan() 主动触发全量扫描。

   附带好处：原生 API 直接给出 0~100 的信号质量与 SSID 二进制，
   不用再去解析「信号 : 94%」这类本地化文本（中英文 Windows 字段名还不一样）。

涉及的结构体都让 ctypes 自己算布局（字段都是 4 字节对齐，与 MSVC 一致）。
"""
import ctypes
import ctypes.wintypes as wt
import time

ERROR_SUCCESS = 0
WLAN_CLIENT_VERSION_VISTA = 2

# dwFlags 位
FLAG_CONNECTED = 0x00000001      # 当前就连接在这个网络上
FLAG_HAS_PROFILE = 0x00000002    # 本机已有保存的配置文件

try:
    _wlan = ctypes.WinDLL("wlanapi", use_last_error=True)
    AVAILABLE = True
except Exception:
    _wlan = None
    AVAILABLE = False


# ------------------------------------------------------------------ 结构体
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", GUID),
                ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", wt.DWORD)]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wt.DWORD), ("dwIndex", wt.DWORD),
                ("InterfaceInfo", WLAN_INTERFACE_INFO * 1)]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
    _fields_ = [
        ("strProfileName", ctypes.c_wchar * 256),
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.c_uint),
        ("uNumberOfBssids", ctypes.c_ulong),
        ("bNetworkConnectable", wt.BOOL),
        ("wlanNotConnectableReason", ctypes.c_ulong),
        ("uNumberOfPhyTypes", ctypes.c_ulong),
        ("dot11PhyTypes", ctypes.c_uint * 8),
        ("bMorePhyTypes", wt.BOOL),
        ("wlanSignalQuality", ctypes.c_ulong),      # 0 ~ 100
        ("bSecurityEnabled", wt.BOOL),
        ("dot11DefaultAuthAlgorithm", ctypes.c_uint),
        ("dot11DefaultCipherAlgorithm", ctypes.c_uint),
        ("dwFlags", ctypes.c_ulong),
        ("dwReserved", ctypes.c_ulong),
    ]


class WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wt.DWORD), ("dwIndex", wt.DWORD),
                ("Network", WLAN_AVAILABLE_NETWORK * 1)]


# ---------------------------------------------- 无线电（Wi-Fi 开关）相关
class WLAN_PHY_RADIO_STATE(ctypes.Structure):
    _fields_ = [("dwPhyIndex", ctypes.c_ulong),
                ("dot11SoftwareRadioState", ctypes.c_uint),   # 软件开关：1=开 2=关
                ("dot11HardwareRadioState", ctypes.c_uint)]   # 硬件开关/飞行模式


class WLAN_RADIO_STATE(ctypes.Structure):
    _fields_ = [("dwNumberOfPhys", ctypes.c_ulong),
                ("PhyRadioState", WLAN_PHY_RADIO_STATE * 64)]


# ---------------------------------------------- 当前连接（取 SSID 用，零进程开销）
class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.c_uint),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11PhyType", ctypes.c_uint),
        ("uDot11PhyIndex", ctypes.c_ulong),
        ("wlanSignalQuality", ctypes.c_ulong),
        ("ulRxRate", ctypes.c_ulong),
        ("ulTxRate", ctypes.c_ulong),
    ]


class WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("bSecurityEnabled", wt.BOOL),
        ("bOneXEnabled", wt.BOOL),
        ("dot11AuthAlgorithm", ctypes.c_uint),
        ("dot11CipherAlgorithm", ctypes.c_uint),
    ]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("isState", ctypes.c_uint),              # WLAN_INTERFACE_STATE
        ("wlanConnectionMode", ctypes.c_uint),   # WLAN_CONNECTION_MODE
        ("strProfileName", ctypes.c_wchar * 256),
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
    ]


WLAN_INTF_OPCODE_INTERFACE_STATE = 0
WLAN_INTERFACE_STATE_CONNECTED = 1     # WLAN_INTERFACE_STATE 里表示已关联
WLAN_INTF_OPCODE_RADIO_STATE = 4
WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7
DOT11_RADIO_STATE_UNKNOWN = 0
DOT11_RADIO_STATE_ON = 1
DOT11_RADIO_STATE_OFF = 2


if AVAILABLE:
    _H = wt.HANDLE
    _wlan.WlanOpenHandle.argtypes = [wt.DWORD, ctypes.c_void_p,
                                     ctypes.POINTER(wt.DWORD), ctypes.POINTER(_H)]
    _wlan.WlanOpenHandle.restype = wt.DWORD
    _wlan.WlanCloseHandle.argtypes = [_H, ctypes.c_void_p]
    _wlan.WlanCloseHandle.restype = wt.DWORD
    _wlan.WlanEnumInterfaces.argtypes = [_H, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST))]
    _wlan.WlanEnumInterfaces.restype = wt.DWORD
    _wlan.WlanScan.argtypes = [_H, ctypes.POINTER(GUID), ctypes.POINTER(DOT11_SSID),
                               ctypes.c_void_p, ctypes.c_void_p]
    _wlan.WlanScan.restype = wt.DWORD
    _wlan.WlanGetAvailableNetworkList.argtypes = [
        _H, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST))]
    _wlan.WlanGetAvailableNetworkList.restype = wt.DWORD
    _wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    _wlan.WlanFreeMemory.restype = None
    _wlan.WlanQueryInterface.argtypes = [
        _H, ctypes.POINTER(GUID), ctypes.c_uint, ctypes.c_void_p,
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint)]
    _wlan.WlanQueryInterface.restype = wt.DWORD
    _wlan.WlanSetInterface.argtypes = [
        _H, ctypes.POINTER(GUID), ctypes.c_uint, wt.DWORD,
        ctypes.c_void_p, ctypes.c_void_p]
    _wlan.WlanSetInterface.restype = wt.DWORD


def _decode_ssid(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _interface_guid(handle):
    plist = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
    rc = _wlan.WlanEnumInterfaces(handle, None, ctypes.byref(plist))
    if rc != ERROR_SUCCESS or not plist:
        return None
    try:
        if plist.contents.dwNumberOfItems == 0:
            return None
        base = ctypes.addressof(plist.contents) + 2 * ctypes.sizeof(wt.DWORD)
        info = WLAN_INTERFACE_INFO.from_address(base)
        return GUID.from_buffer_copy(info.InterfaceGuid)
    finally:
        _wlan.WlanFreeMemory(plist)


def _available(handle, guid):
    plist = ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST)()
    rc = _wlan.WlanGetAvailableNetworkList(handle, ctypes.byref(guid), 0, None,
                                           ctypes.byref(plist))
    if rc != ERROR_SUCCESS or not plist:
        return []
    try:
        n = plist.contents.dwNumberOfItems
        base = ctypes.addressof(plist.contents) + 2 * ctypes.sizeof(wt.DWORD)
        step = ctypes.sizeof(WLAN_AVAILABLE_NETWORK)
        found, seen = [], set()
        for i in range(n):
            net = WLAN_AVAILABLE_NETWORK.from_address(base + i * step)
            ln = int(net.dot11Ssid.uSSIDLength)
            if ln <= 0 or ln > 32:
                continue                        # 隐藏网络
            ssid = _decode_ssid(bytes(net.dot11Ssid.ucSSID[:ln]))
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            flags = int(net.dwFlags)
            found.append({
                "ssid": ssid,
                "signal": int(net.wlanSignalQuality),
                "connectable": bool(net.bNetworkConnectable),
                "secure": bool(net.bSecurityEnabled),
                "profile": (net.strProfileName or "").strip(),
                "connected": bool(flags & FLAG_CONNECTED),
                "has_profile": bool(flags & FLAG_HAS_PROFILE),
            })
        return found
    finally:
        _wlan.WlanFreeMemory(plist)


def scan_networks(max_wait=5.0, poll=0.6, min_wait=1.8):
    """
    主动触发一次全量扫描并返回所有可见网络。

    返回 [{"ssid","signal","connectable","secure","profile"}, ...]
    出错或不可用时返回 []（调用方可以退回到 netsh 解析）。
    """
    if not AVAILABLE:
        return []
    handle = wt.HANDLE()
    version = wt.DWORD()
    rc = _wlan.WlanOpenHandle(WLAN_CLIENT_VERSION_VISTA, None,
                              ctypes.byref(version), ctypes.byref(handle))
    if rc != ERROR_SUCCESS:
        return []
    try:
        guid = _interface_guid(handle)
        if guid is None:
            return []
        # pDot11Ssid = NULL → 扫描所有网络（这才是全量扫描）
        _wlan.WlanScan(handle, ctypes.byref(guid), None, None, None)

        best, stable, t0 = [], 0, time.time()
        while True:
            items = _available(handle, guid)
            if len(items) > len(best):
                best, stable = items, 0
            else:
                stable += 1
            elapsed = time.time() - t0
            if elapsed >= max_wait:
                break
            if stable >= 2 and elapsed >= min_wait:
                break                          # 连续两次没变多，视为扫完
            time.sleep(poll)
        return best
    except Exception:
        return []
    finally:
        try:
            _wlan.WlanCloseHandle(handle, None)
        except Exception:
            pass


def _open():
    """打开 WLAN 句柄；返回 (handle 或 None, 返回码)"""
    handle = wt.HANDLE()
    version = wt.DWORD()
    rc = _wlan.WlanOpenHandle(WLAN_CLIENT_VERSION_VISTA, None,
                              ctypes.byref(version), ctypes.byref(handle))
    return (handle, rc) if rc == ERROR_SUCCESS else (None, rc)


def _query(handle, guid, opcode):
    """调用 WlanQueryInterface，返回 (原始字节 或 None, 返回码)"""
    size = wt.DWORD()
    data = ctypes.c_void_p()
    otype = ctypes.c_uint()
    rc = _wlan.WlanQueryInterface(handle, ctypes.byref(guid), opcode, None,
                                  ctypes.byref(size), ctypes.byref(data),
                                  ctypes.byref(otype))
    if rc != ERROR_SUCCESS or not data:
        return None, rc
    try:
        return ctypes.string_at(data, size.value), rc
    finally:
        _wlan.WlanFreeMemory(data)


def _read_radio_state(handle, guid):
    raw, rc = _query(handle, guid, WLAN_INTF_OPCODE_RADIO_STATE)
    if raw is None:
        return None, rc
    need = ctypes.sizeof(WLAN_RADIO_STATE)
    if len(raw) < need:                      # 实际返回可能比结构体小，补零
        raw = raw + b"\x00" * (need - len(raw))
    return WLAN_RADIO_STATE.from_buffer_copy(raw), rc


def is_wifi_available():
    """有没有可用的 WLAN 接口（网卡是否处于启用状态）"""
    if not AVAILABLE:
        return False
    handle, rc = _open()
    if handle is None:
        return False
    try:
        return _interface_guid(handle) is not None
    finally:
        _wlan.WlanCloseHandle(handle, None)


def ensure_radio_on():
    """
    确保 Wi-Fi 无线电（软开关）处于打开状态。

    返回 (结果, 说明)：
        True  = 已经是开的；或本次成功打开
        False = 试图打开但失败 / 没有可用的 WLAN 接口
        None  = 无法判断（wlanapi 不可用）

    说明文字为空字符串表示「本来就开着，什么都没做」。
    注意：走的是**当前登录用户**的 WLAN 会话，**不需要管理员权限**。
    硬件开关 / 飞行模式被关时软件开关打不开，会在说明里点出来。
    """
    if not AVAILABLE:
        return None, "wlanapi 不可用"
    handle, rc = _open()
    if handle is None:
        return None, "无法打开 WLAN 句柄 (rc=%d)" % rc
    try:
        guid = _interface_guid(handle)
        if guid is None:
            return False, "没有可用的 WLAN 接口（无线网卡被禁用，或 WLAN AutoConfig 服务未运行）"
        st, rc = _read_radio_state(handle, guid)
        if st is None:
            return None, "查询 Wi-Fi 无线电状态失败 (rc=%d)" % rc
        n = min(int(st.dwNumberOfPhys), 64)
        if n == 0:
            return None, "没有可用的物理层"

        if all(st.PhyRadioState[i].dot11SoftwareRadioState == DOT11_RADIO_STATE_ON
               for i in range(n)):
            return True, ""                          # 本来就开着，什么都不做

        hw_off = [i for i in range(n)
                  if st.PhyRadioState[i].dot11HardwareRadioState == DOT11_RADIO_STATE_OFF]

        # ⚠ 关键：设置无线电状态时，pData 必须是 **WLAN_PHY_RADIO_STATE**（12 字节），
        #   而且要为每个 PHY 单独下发一次，dwPhyIndex 指明改哪一个。
        #   查询用的是 WLAN_RADIO_STATE（772 字节）——把那个整个塞进 Set 会直接
        #   返回 ERROR_INVALID_PARAMETER(87)。两个结构体名字极像，是经典坑。
        failed = []
        for i in range(n):
            phy = WLAN_PHY_RADIO_STATE()
            phy.dwPhyIndex = i
            phy.dot11SoftwareRadioState = DOT11_RADIO_STATE_ON
            # 硬件开关状态保持查询到的原值，不要乱改（改它没有意义，且可能被拒）
            phy.dot11HardwareRadioState = st.PhyRadioState[i].dot11HardwareRadioState
            rc = _wlan.WlanSetInterface(handle, ctypes.byref(guid),
                                        WLAN_INTF_OPCODE_RADIO_STATE,
                                        ctypes.sizeof(WLAN_PHY_RADIO_STATE),
                                        ctypes.byref(phy), None)
            if rc != ERROR_SUCCESS:
                failed.append((i, rc))

        if len(failed) == n:
            return False, "打开 Wi-Fi 失败 (rc=%d)" % failed[0][1]
        if failed:
            return False, "部分物理层打开失败: %s" % failed

        if hw_off:
            return False, "已尝试打开 Wi-Fi，但硬件开关/飞行模式仍关闭（请检查机身开关或飞行模式）"

        # 复查一次，确认真的打开了（有些机器下发成功但被策略/驱动顶回去）
        st2, _rc2 = _read_radio_state(handle, guid)
        if st2 is not None:
            m = min(int(st2.dwNumberOfPhys), 64)
            if m and not all(st2.PhyRadioState[j].dot11SoftwareRadioState == DOT11_RADIO_STATE_ON
                             for j in range(m)):
                return False, "已下发打开指令，但无线电仍未开启（可能被硬件开关或系统策略阻止）"
        return True, "已自动打开 Wi-Fi"
    except Exception as e:
        return None, "打开 Wi-Fi 时出错: %s: %s" % (type(e).__name__, e)
    finally:
        _wlan.WlanCloseHandle(handle, None)


# ⚠️ 踩坑记录（2026-09-24 实测）：想用「无线网卡当前有没有关联」来判断网络是否就绪，
#    试过 `wlan_intf_opcode_interface_state`(0)，结果 **rc = 50 = ERROR_NOT_SUPPORTED**
#    —— 这个 opcode 在现代 Windows 上就是被标注为「不支持」的，别再试。
#    而且它即使能用也只是「有没有关联」，不如直接判断「认证网关可达」精确
#    （后者是纯 TCP，同样不涉及位置信息）。
#
#    所以启动阶段不在这里等「就绪」，而是：开完 Wi-Fi 直接进主循环发起连接
#    —— 因为「连接」这个动作本身才是让网络变就绪的原因。


# ------------------------------------------------------------------ 「开关」与
# 「有没有连上」的零位置访问判断
#
# ⚠ 背景（很重要）：Windows 把「**读取 Wi-Fi 信息**」算作一次位置访问。
#   实测 `WlanQueryInterface(wlan_intf_opcode_current_connection)`（取当前 SSID）
#   会触发，而下面这两个判断**不会**，因为它们问的都不是"Wi-Fi 的细节"：
#     · 无线电开关：wlan_intf_opcode_radio_state（实测不触发）
#     · 是否已连上：走 IP Helper 的 GetAdaptersAddresses，只问 IP 层网卡起没起来
#   监控循环每 10 秒就跑一次，所以必须用这两个零成本判断，
#   把昂贵的「取 SSID」留到真正需要它的时候（掉线、准备登录）。

IF_TYPE_IEEE80211 = 71
IF_OPER_STATUS_UP = 1
AF_INET = 2
AF_INET6 = 23
AF_UNSPEC = 0
_GAA_FLAG_SKIP_ANYCAST = 0x02
_GAA_FLAG_SKIP_MULTICAST = 0x04
_GAA_FLAG_SKIP_DNS_SERVER = 0x08
_ERROR_BUFFER_OVERFLOW = 111
_ERROR_NO_DATA = 232


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p),
                ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wt.ULONG),
    ("Flags", wt.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", wt.ULONG),
    ("PreferredLifetime", wt.ULONG),
    ("LeaseLifetime", wt.ULONG),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wt.ULONG),
    ("IfIndex", wt.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wt.ULONG),
    ("Flags", wt.ULONG),
    ("Mtu", wt.ULONG),
    ("IfType", wt.DWORD),
    ("OperStatus", ctypes.c_int),
]


def _iphlpapi():
    try:
        d = ctypes.WinDLL("iphlpapi", use_last_error=True)
        d.GetAdaptersAddresses.argtypes = [wt.ULONG, wt.ULONG, ctypes.c_void_p,
                                           ctypes.POINTER(IP_ADAPTER_ADDRESSES),
                                           ctypes.POINTER(wt.ULONG)]
        d.GetAdaptersAddresses.restype = wt.ULONG
        return d
    except Exception:
        return None


_iphlp = _iphlpapi() if AVAILABLE else None


def radio_state():
    """
    只读 Wi-Fi 无线电（开关）状态：True=开着 / False=关着 / None=查不到。

    **不涉及位置信息**（实测），所以在监控循环里可以每 10 秒问一次 ——
    这正是"用户手动关掉 Wi-Fi 时不要自作主张去打开"所依赖的信号。
    """
    if not AVAILABLE:
        return None
    handle, _rc = _open()
    if handle is None:
        return None
    try:
        guid = _interface_guid(handle)
        if guid is None:
            return None
        st, _rc2 = _read_radio_state(handle, guid)
        if st is None:
            return None
        n = min(int(st.dwNumberOfPhys), 64)
        if n == 0:
            return None
        return all(st.PhyRadioState[i].dot11SoftwareRadioState == DOT11_RADIO_STATE_ON
                   for i in range(n))
    except Exception:
        return None
    finally:
        try:
            _wlan.WlanCloseHandle(handle, None)
        except Exception:
            pass


def _has_ipv4(adapter):
    """这个网卡有没有拿到 IPv4 地址（地址还在不在有效期内不看，够用就行）"""
    p = adapter.FirstUnicastAddress
    seen = 0
    while p and seen < 32:                     # 防御性上限，避免链表异常时死循环
        try:
            sa = p.contents.Address
            if sa.lpSockaddr:
                fam = ctypes.cast(sa.lpSockaddr,
                                  ctypes.POINTER(ctypes.c_ushort)).contents.value
                if fam == AF_INET:
                    return True
        except Exception:
            return False
        p = p.contents.Next
        seen += 1
    return False


def _is_virtual_wifi(a):
    """
    是不是「Wi-Fi Direct 虚拟网卡」。

    ⚠ 本机实测踩坑：Intel 网卡会额外挂两个
    `Microsoft Wi-Fi Direct Virtual Adapter`，它们的 IfType 同样是 71（IEEE802.11），
    但状态常年是 Down。如果按「遇到第一个 802.11 就下结论」写，
    就永远得到"没连上"——**真实的物理网卡排在它们后面**。
    所以要先认出来并跳过（它们只在投屏/Miracast 时才 Up）。
    """
    d = ("%s %s" % (a.Description or "", a.FriendlyName or "")).lower()
    return ("virtual" in d) or ("direct" in d) or ("虚拟" in d)


def wifi_link_state():
    """
    判断「Wi-Fi 到底连上没有」——**不涉及位置信息、不起子进程**。

    走 IP Helper 的 GetAdaptersAddresses，找类型为 IEEE 802.11 的**物理**适配器，
    看它的 OperStatus 与有没有 IPv4 地址：

        "up"       已关联且拿到 IPv4（已经连上某个 WiFi）
        "linking"  已关联但还没拿到地址（正在获取 IP —— 别去打断它）
        "down"     没连上任何 WiFi（断开，或无线电关着）
        None       查不到（没有无线网卡 / 系统调用失败）

    为什么不用 WLAN API 的「当前连接」：那个返回值里带 SSID，会触发位置访问。
    这里只问 IP 层「网卡起没起来」，既够用又零成本 —— 监控循环每 10 秒要问一次。
    """
    if _iphlp is None:
        return None
    flags = (_GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST
             | _GAA_FLAG_SKIP_DNS_SERVER)
    size = wt.ULONG(16 * 1024)
    for _ in range(4):                          # 缓冲区不够就翻倍重试
        buf = ctypes.create_string_buffer(size.value)
        rc = _iphlp.GetAdaptersAddresses(
            AF_UNSPEC, flags, None,
            ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
            ctypes.byref(size))
        if rc == _ERROR_BUFFER_OVERFLOW:
            continue
        if rc == _ERROR_NO_DATA:
            return None                         # 一个网卡都没有
        if rc != 0:
            return None

        phys, virt = [], []
        p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
        n = 0
        while p and n < 64:
            n += 1
            a = p.contents
            if int(a.IfType) == IF_TYPE_IEEE80211:
                (virt if _is_virtual_wifi(a) else phys).append(a)
            p = a.Next

        for group in (phys, virt):
            if not group:
                continue
            ups = [a for a in group if int(a.OperStatus) == IF_OPER_STATUS_UP]
            if not ups:
                return "down"
            # 有起来的：拿到 IP 才算真连上，否则还在获取地址
            return "up" if any(_has_ipv4(a) for a in ups) else "linking"
        return None                             # 一个 802.11 网卡都没有
    return None


def current_ssid():
    """
    用原生 WLAN API 取当前连接的 SSID —— **完全不启动任何子进程**。

    以前这一步是调 `netsh wlan show interfaces` 解析文本，每轮检测都要起一个
    netsh.exe。虽然用完即退，但用户会在任务管理器里看到「网络命令外壳」一闪而过，
    而且起进程本身也要几十毫秒。改走 API 后这两个问题都没了。

    返回：
        None  = API 不可用 / 查询失败（调用方应回退到 netsh）
        ""    = 确实没有连接任何网络
        "xxx" = 当前 SSID
    """
    if not AVAILABLE:
        return None
    handle, _rc = _open()
    if handle is None:
        return None
    try:
        guid = _interface_guid(handle)
        if guid is None:
            return None
        raw, rc = _query(handle, guid, WLAN_INTF_OPCODE_CURRENT_CONNECTION)
        if raw is None or rc != ERROR_SUCCESS:
            return None
        need = ctypes.sizeof(WLAN_CONNECTION_ATTRIBUTES)
        if len(raw) < need:
            return None
        conn = WLAN_CONNECTION_ATTRIBUTES.from_buffer_copy(raw[:need])
        n = int(conn.wlanAssociationAttributes.dot11Ssid.uSSIDLength)
        if n <= 0 or n > 32:
            return ""                       # 未连接
        return _decode_ssid(bytes(conn.wlanAssociationAttributes.dot11Ssid.ucSSID[:n]))
    except Exception:
        return None
    finally:
        try:
            _wlan.WlanCloseHandle(handle, None)
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("wlanapi 可用:", AVAILABLE)
    print("WLAN_AVAILABLE_NETWORK 结构体大小:", ctypes.sizeof(WLAN_AVAILABLE_NETWORK), "字节")
    t0 = time.time()
    nets = scan_networks()
    print("扫描耗时 %.2f 秒，共 %d 个" % (time.time() - t0, len(nets)))
    for n in sorted(nets, key=lambda d: -d["signal"]):
        print("   %-28s %3d%%  %s%s" % (n["ssid"], n["signal"],
                                       "开放" if not n["secure"] else "加密",
                                       "  有配置文件" if n["profile"] else ""))
