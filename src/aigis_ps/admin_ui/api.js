export const API_ROOT = "/admin/api/v1";
export const STATIC_ROOT = "/admin/static";

export const DESKTOP_ROUTES = Object.freeze({
    status: "/desktop",
    startService: "/desktop/service/start",
    stopService: "/desktop/service/stop",
    startGame: "/desktop/game/start",
    createProfile: "/desktop/profiles",
    selectProfile: "/desktop/profiles/select",
});

export const ROUTES = Object.freeze({
    session: "/session",
    runtime: "/runtime",
    readiness: "/readiness/{domain}",
    commands: "/commands",
    plans: "/plans",
    commits: "/commits",
    audit: "/audit",
    failures: "/audit/failures",
    requests: "/requests",
    views: "/views",
    view: "/views/{name}",
    pages: "/ui-pages",
    backups: "/backups",
    restorePreview: "/restores/preview",
    restores: "/restores",
    exports: "/exports",
    config: "/config",
    validations: "/config/validations",
    job: "/jobs/{job_id}",
    switches: "/config/switches",
    unstage: "/config/unstage",
    content: "/content",
    contentCaptures: "/content/captures",
    contentImports: "/content/imports",
    contentValidations: "/content/validations",
    contentUnstage: "/content/unstage",
});

export const TOKEN_STATE = Object.freeze({ PRESENT: "present", MISSING: "missing", MALFORMED: "malformed" });
export const PLAN_CAPACITY = 32;
export const PLAN_TTL_SECONDS = 600;
export const CANDIDATE_TTL_SECONDS = 600;
export const AUDIT_FIELD_BYTES = 1024 * 1024;
export const AUTO_BACKUP_KEEP = 1;
export const ADMIT_TIMEOUT_SECONDS = 10;

const TOKEN_PARAMETER = "token";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,256}$/;
const ROUTE_VALUE = /\{([a-z_]+)\}/g;
const JSON_TYPE = "application/json";
const NOT_MODIFIED = 304;
const UNAUTHORIZED = 401;
const CLIENT_ERRORS = { from: 400, to: 499 };
const TOKEN_MISSING = "console_token_missing";
const NETWORK_ERROR = "console_network_error";
const RESPONSE_INVALID = "console_response_invalid";
const BYTES_PER_MIB = 1024 * 1024;
// The server raises these 5xx refusals before it writes anything, so they settle an operation like a 4xx answer.
const UNWRITTEN_REFUSALS = new Set(["admin_backup_failed", "admin_command_unavailable", "config_switch_unavailable",
    "generation_switching"]);

const ERROR_HINTS = Object.freeze({
    [TOKEN_MISSING]: "地址里没有管理令牌。请运行 open-console.ps1，或打开服务启动时打印的带 #token= 的地址。",
    [NETWORK_ERROR]: "请求没有得到服务端的答复，结果未知。重试会沿用同一个操作 ID，不会重复执行。",
    [RESPONSE_INVALID]: "服务端的答复无法解析，结果未知。重试会沿用同一个操作 ID，不会重复执行。",
    admin_unauthorized: "令牌无效或已更换：每次启动服务都会生成新令牌。请重新运行 open-console.ps1。",
    admin_params_invalid: "参数不合规，请按标出的字段修改。",
    admin_command_unavailable: "当前世代缺少这条命令依赖的服务。",
    admin_revision_conflict: "预览之后存档已被修改（可能是游戏写入），请重新预览。",
    plan_expired: `计划已过期或被挤出（最多保留 ${PLAN_CAPACITY} 份、${PLAN_TTL_SECONDS} 秒），请重新预览。`,
    plan_changed: "计划与当前状态不再一致，请重新预览。",
    plan_stale: "世代、内容版本或 JST 日界已变化，请重新预览。",
    admin_operation_conflict: "这个操作 ID 已用于另一项操作，请重新预览。",
    admin_confirmation_required: "确认短语必须逐字输入。",
    admin_client_closed_required: "需要先确认游戏客户端已关闭。",
    admin_client_active: "静默阈值内仍有游戏请求（阈值见状态页或报错详情的 quiet_seconds）。请关闭客户端，等待后再试。",
    admin_restart_incompatible: "重启兼容检查未通过：服务重启时会拒绝或改写这个状态。",
    admin_preflight_failed: "预检未通过：客户端回读这些数据时会出错。",
    admin_battle_abort_required: "存在进行中的战斗，需要先中止。",
    admin_restore_clock_required: "备份缺少能量时钟标记，需先离线执行 energy-clock-plan 与 energy-clock-apply。",
    admin_backup_failed: "自动备份失败，没有写入任何内容。",
    admin_backup_busy: "备份旁边还有 -wal/-shm 日志文件，清理之后才能读取。",
    admin_backup_path_invalid: "备份必须是数据目录内的普通文件，且不能是运行中的库文件本身。",
    admin_audit_too_large: `审计记录超过存储上限（每个字段 ${AUDIT_FIELD_BYTES / BYTES_PER_MIB} MiB），没有提交任何内容；请拆小批次或缩短参数。`,
    admin_gold_current_changed: "存档金币与 current 不一致：刷新余额（表单会随之改写 current），或把 current 改为详情里的 money（当前余额）后重新预览。",
    admin_view_unknown: "服务端没有提供这个视图。",
    admin_view_service_unavailable: "当前世代缺少这个视图依赖的服务。",
    admin_tool_unit_missing: "没有这个 GetIndex 的单位，请先在单位页核对。",
    admin_purchase_flag_ack_required: "这次修改涉及服装商店的购买标志：确认详情里列出的商品后，把 purchase_flag_ack 设为 true 再预览。",
    admin_background_rows_unsupported: "这些背景行不是目录里可选的已核实背景，原因见详情里的 findings。",
    admin_background_save_time_future: "SaveTime 不能晚于服务器的 JST 时间（详情里的 now）。",
    generation_switching: "服务正在切换世代，请稍后重试。",
    generation_draining: "上一个世代仍在处理请求，请稍后重试。",
    generation_build_busy: "另一个候选世代正在构建，请等它完成后再校验。",
    switch_drain_timeout: "在途请求没有及时完成，本次切换已放弃。请在客户端空闲时重试。",
    config_switch_unavailable: "配置切换尚未接入当前服务进程。",
    config_switch_busy: "另一项配置切换正在进行。",
    config_staged_pending: "已有暂存配置等待下次启动，请先应用或撤销暂存。",
    config_key_overridden: "这些键被命令行规则文件覆盖，本进程内只读。",
    config_job_unknown: "这个校验作业已被新的校验替换或已过期，请重新校验。",
    config_report_expired: "校验报告已失效，请重新校验。",
    config_report_stale: "报告之后运行中的世代、配置或主表指针、存档已变化，请刷新报告或重新校验。",
    config_tier_changed: "存档变化后这次切换需要的档位已经不同，请重新校验。",
    config_unchanged: "候选配置与正在运行或已指向的配置相同，不需要切换。",
    config_stage_unsupported: "含时间累积规则、料理阈值或新手奖励总数变化的切换只能在运行中应用，不能暂存。",
    config_candidate_blocked: "服务会拒绝以这份配置启动，请查看启动闸门问题。",
    config_candidate_master_mismatch: "候选世代加载的主表与请求的快照不一致，请重新校验。",
    config_not_staged: "没有等待下次启动的暂存配置集，请刷新配置状态。",
    config_staged_changed: "暂存的配置集已经变了，请刷新配置状态后再撤销。",
    config_unstage_empty: "撤销暂存需要指明配置集或主表快照。",
    generation_candidate_missing: "候选世代已过期或被替换，请重新校验。",
    masterdata_snapshot_unknown: "数据目录里没有这个主表快照，请刷新快照列表。",
    masterdata_invalid: "主表快照或指针文件无法读取，或没有通过校验。",
    masterdata_pointer_conflict: "主表指针在这期间被改动，请刷新后重试。",
    masterdata_pointer_busy: "另一项主表激活正在进行，请稍后重试。",
    masterdata_not_staged: "没有等待下次启动的主表暂存，请刷新配置状态。",
    masterdata_staged_changed: "暂存的主表快照已经变了，请刷新配置状态后再撤销。",
    masterdata_unstage_unsupported: "运行中的世代没有主表快照，无法把下次启动指回当前主表。",
    content_capture_unknown: "采集目录不存在，请刷新采集列表。",
    content_capture_invalid: "采集目录不完整，或者与客户端指纹不符，不能导入。",
    content_captures_unreadable: "采集目录无法列出：目录是链接、联接，或者没有读取权限。",
    content_import_failed: "采集无法读取，或者无法写入数据目录，没有导入任何快照。",
    login_cooking_quota_below_issued: "本月已发料理份数高于新阈值算出的额度，下个业务月再迁移。",
    login_cooking_clock_backwards: "迁移时刻早于存档的时间水位，请检查服务器时钟。",
});

export class ApiError extends Error {
    constructor({ status = 0, code, message, fields = [], currentRevision = null, details = {}, definite }) {
        super(message);
        this.name = "ApiError";
        this.status = status;
        this.code = code;
        this.fields = fields;
        this.currentRevision = currentRevision;
        this.details = details;
        // Only a decided answer settles an operation; anything else may have been applied before the reply was lost.
        this.definite = definite;
        this.hint = errorHint(code);
    }
}

export function errorHint(code) {
    return Object.hasOwn(ERROR_HINTS, code) ? ERROR_HINTS[code] : null;
}

const credentials = adoptToken();
const unauthorizedListeners = new Set();
let serviceContext = null;

export function setServiceContext(identity, available, profileId = null) {
    if (serviceContext?.identity === identity && serviceContext.available === available
        && serviceContext.profileId === profileId) return false;
    serviceContext?.controller.abort();
    serviceContext = { identity, available, profileId, controller: new AbortController() };
    return true;
}

function adoptToken() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (!fragment.has(TOKEN_PARAMETER)) return Object.freeze({ token: null, state: TOKEN_STATE.MISSING });
    const candidate = fragment.get(TOKEN_PARAMETER);
    // Dropping the fragment keeps the token out of the address bar, the history entry and every later navigation.
    history.replaceState(null, "", location.pathname + location.search);
    return TOKEN_PATTERN.test(candidate)
        ? Object.freeze({ token: candidate, state: TOKEN_STATE.PRESENT })
        : Object.freeze({ token: null, state: TOKEN_STATE.MALFORMED });
}

export function tokenState() {
    return credentials.state;
}

export function onUnauthorized(listener) {
    unauthorizedListeners.add(listener);
    return () => unauthorizedListeners.delete(listener);
}

function address(route, values, query) {
    const path = route.replace(ROUTE_VALUE, (placeholder, name) => {
        if (!Object.hasOwn(values, name)) throw new TypeError(`The route ${route} needs ${name}`);
        return encodeURIComponent(String(values[name]));
    });
    const pairs = Object.entries(query ?? {}).filter(([, value]) => value !== undefined && value !== null);
    return pairs.length ? `${API_ROOT}${path}?${new URLSearchParams(pairs)}` : `${API_ROOT}${path}`;
}

function failure(status, payload) {
    const error = payload !== null && typeof payload === "object" ? payload.error : null;
    if (error === null || typeof error !== "object" || typeof error.code !== "string") {
        return new ApiError({ status, code: `admin_http_${status}`, message: `HTTP ${status}`, definite: false });
    }
    return new ApiError({
        status,
        code: error.code,
        message: String(error.message ?? ""),
        fields: Array.isArray(error.fields) ? error.fields : [],
        currentRevision: Number.isInteger(error.current_revision) ? error.current_revision : null,
        details: error.details !== null && typeof error.details === "object" ? error.details : {},
        definite: (status >= CLIENT_ERRORS.from && status <= CLIENT_ERRORS.to) || UNWRITTEN_REFUSALS.has(error.code),
    });
}

async function payloadOf(response) {
    const text = await response.text();
    try {
        return JSON.parse(text);
    } catch {
        return undefined;
    }
}

async function send(method, route, { values = {}, query, body, etag, signal } = {}) {
    if (credentials.token === null) {
        throw new ApiError({ code: TOKEN_MISSING, message: "No admin token is available", definite: true });
    }
    const scoped = serviceContext !== null && route !== ROUTES.session && route !== ROUTES.pages
        && !Object.values(DESKTOP_ROUTES).includes(route);
    if (scoped) {
        if (!serviceContext.available) {
            throw new ApiError({ code: "desktop_service_unavailable", message: "请先在启动页开启服务。", definite: true });
        }
        signal = signal ? AbortSignal.any([signal, serviceContext.controller.signal]) : serviceContext.controller.signal;
    }
    signal?.throwIfAborted();
    const headers = { Accept: JSON_TYPE, Authorization: `Bearer ${credentials.token}` };
    if (scoped && serviceContext.profileId !== null) headers["X-Aigis-Profile"] = serviceContext.profileId;
    if (etag) headers["If-None-Match"] = etag;
    const init = {
        method, headers, signal, mode: "same-origin", credentials: "omit", cache: "no-store", redirect: "error",
        // Fetch serializes the Origin of a no-referrer POST as null, which the server's exact-origin check refuses.
        referrerPolicy: "same-origin",
    };
    if (body !== undefined) {
        headers["Content-Type"] = JSON_TYPE;
        init.body = JSON.stringify(body);
    }
    let response;
    try {
        response = await fetch(address(route, values, query), init);
    } catch (error) {
        if (signal?.aborted) throw error;
        throw new ApiError({ code: NETWORK_ERROR, message: String(error?.message ?? error), definite: false });
    }
    if (response.status === NOT_MODIFIED) {
        return { notModified: true, data: null, etag: response.headers.get("ETag") ?? etag };
    }
    const payload = await payloadOf(response);
    signal?.throwIfAborted();
    if (!response.ok) {
        const error = failure(response.status, payload);
        if (response.status === UNAUTHORIZED) unauthorizedListeners.forEach((listener) => listener(error));
        throw error;
    }
    if (payload === undefined) {
        throw new ApiError({ status: response.status, code: RESPONSE_INVALID, message: "The reply is not JSON",
            definite: false });
    }
    return { notModified: false, data: payload, etag: response.headers.get("ETag") };
}

export const api = Object.freeze({
    get: async (route, options) => (await send("GET", route, options)).data,
    post: async (route, body, options) => (await send("POST", route, { ...options, body })).data,
    read: (route, options) => send("GET", route, options),
    view: (name, params, options = {}) => send("GET", ROUTES.view, {
        ...options, values: { name }, query: params === undefined ? undefined : { params: JSON.stringify(params) },
    }),
});

export function isDefinite(error) {
    return error instanceof ApiError && error.definite;
}

function delay(milliseconds, signal, wake) {
    return new Promise((resolve) => {
        if (signal.aborted || wake.requested) {
            wake.requested = false;
            resolve();
            return;
        }
        const timer = milliseconds === null ? null : setTimeout(done, milliseconds);
        signal.addEventListener("abort", done, { once: true });
        wake.resolve = done;
        function done() {
            if (timer !== null) clearTimeout(timer);
            signal.removeEventListener("abort", done);
            wake.resolve = null;
            resolve();
        }
    });
}

function visible(signal) {
    return new Promise((resolve) => {
        const done = () => {
            if (document.hidden && !signal.aborted) return;
            document.removeEventListener("visibilitychange", done);
            signal.removeEventListener("abort", done);
            resolve();
        };
        document.addEventListener("visibilitychange", done);
        signal.addEventListener("abort", done, { once: true });
        done();
    });
}

export function watch(load, { interval, signal, onData, onError = () => {}, repeat = () => true, retry = () => true }) {
    const wake = { resolve: null, requested: false };
    let etag = null;
    (async () => {
        while (!signal.aborted) {
            wake.requested = false;
            let timed = true;
            try {
                const result = await load(etag);
                if (signal.aborted) return;
                if (!result.notModified) {
                    etag = result.etag;
                    onData(result.data);
                }
                timed = repeat(result);
            } catch (error) {
                if (signal.aborted) return;
                const ended = error instanceof ApiError && error.status === UNAUTHORIZED;
                timed = !ended && retry(error);
                onError(error, { waiting: !ended && !timed });
                if (ended) return;
            }
            await delay(timed ? interval : null, signal, wake);
            // Hidden windows stop polling so a forgotten console never keeps the server busy.
            if (document.hidden) await visible(signal);
        }
    })();
    const refresh = () => {
        // A load already in flight may predate whatever asked for the refresh, so another load follows it at once.
        if (wake.resolve) wake.resolve();
        else wake.requested = true;
    };
    return Object.freeze({ refresh });
}
