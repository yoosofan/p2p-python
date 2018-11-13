

class C:
    # 一度に受け取れる最大データ量(260kBytes)
    MAX_RECEIVE_SIZE = 260000

    # type
    T_SERVER = 'type/server'
    T_CLIENT = 'type/client'

    # Master key
    MASTER_KEYS = []


class V:
    # path
    DATA_PATH = None
    TMP_PATH = None

    # info
    CLIENT_VER = None
    SERVER_NAME = None
    NETWORK_VER = None
    P2P_PORT = None
    P2P_ACCEPT = None
    P2P_UDP_ACCEPT = None

    # setting
    F_FILE_CONTINUE_ASKING = False


class Debug:
    P_EXCEPTION = False  # print exception info
    P_RECEIVE_MSG_INFO = False  # print receive msg info
    F_RECODE_TRAFFIC = False  # recode traffic to file
    F_SEND_CHANNEL_INFO = False  # send to details with channel fnc
    F_LONG_MSG_INFO = False  # long message info


class PeerToPeerError(Exception):
    pass
