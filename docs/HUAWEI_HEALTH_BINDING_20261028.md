# 华为运动健康账号绑定

入口固定在「陪伴小屋 → 感知 → 模型能力 → 华为运动健康账号」。

## 方式一：账号密码登录

1. 在「华为运动健康账号」卡填写华为账号和密码，点「保存账号密码」。
2. 需要无头自动登录时勾选「使用已保存账号密码无头自动登录」。
3. 点「打开浏览器登录」。自动模式会在后台完成；人工模式会打开可见浏览器。
4. 如果华为要求手机验证码，在卡片中填写验证码并提交；要求信任设备时选择信任或不信任。
5. 登录成功后点「拉取最新健康数据」。首次取数会完成 OAuth 并缓存 token。
6. 健康感知总开关会自动打开。后续私聊中，睡眠、静息心率、HRV 和运动记录会作为有界摘要进入上下文。

账号密码只保存在本机 Windows DPAPI 加密文件中，页面不回填密码。换 Windows 用户或换机器后需要重新保存。

## 方式二：Cookie 导入

1. 在电脑浏览器登录 `https://health.cloud.huawei.com/TrainingCamp`，确认能看到自己的健康数据。
2. 用 Cookie-Editor 分别复制六项 Cookie 值：
   - `CASTGC`（`.id1.cloud.huawei.com`）
   - `sid`（`.id1.cloud.huawei.com`）
   - `hwid_cas_sid`（`.id1.cloud.huawei.com`）
   - `HuaweiID_CAS_ISCASLOGIN`（`.huawei.com`）
   - `CASLOGINSITE`（`.huawei.com`）
   - `LOGINACCSITE`（`.huawei.com`）
3. 填到绑定卡对应输入框，或把 Cookie-Editor 导出的整段 JSON 粘到文本框。
4. 点「导入 Cookie」，再点「拉取最新健康数据」。
5. 首次会启动浏览器完成 OAuth，约 30 秒；之后 token 有效期内直接走 HTTP。
6. token 过期后会使用保存的 Cookie 自动续期。华为要求重新验证时，按页面提示重新导入 Cookie。

## 作用范围与边界

- 绑定按当前私聊角色隔离，不会进入群聊或其他私聊窗口。
- 只读取用户本人授权的数据，不控制手表、不修改华为账号。
- 健康摘要不是实时生命体征或医疗诊断；数据为空、过期或接口改版时不会猜测。
- 同一时间只允许一个登录会话；轮换登录材料后旧 token 会失效。
