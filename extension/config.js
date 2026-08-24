// 默认配置（用户在 popup 里覆盖）
export const CONFIG_DEFAULTS = {
  server:    "http://127.0.0.1:8000",
  apiSecret: "",
  poiList: [
    { viewid: 233,  name: "天坛公园",     url: "https://m.ctrip.com/restapi/soa2/14509/json/GetSightOverview?viewId=233" },
    { viewid: 5170, name: "景山公园",     url: "https://m.ctrip.com/restapi/soa2/14509/json/GetSightOverview?viewId=5170" },
    { viewid: 5153, name: "雍和宫",       url: "https://m.ctrip.com/restapi/soa2/14509/json/GetSightOverview?viewId=5153" },
    { viewid: 231,  name: "颐和园",       url: "https://m.ctrip.com/restapi/soa2/14509/json/GetSightOverview?viewId=231" },
    { viewid: 5208, name: "圆明园",       url: "https://m.ctrip.com/restapi/soa2/14509/json/GetSightOverview?viewId=5208" },
  ],
};