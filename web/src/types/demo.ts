// 与 scripts/export_demo_data.py 产出的 demo.json 严格对齐。
// 改这里之前先看 pipeline.py 的 _build_stats / _summarize_event_severity。

export type Severity = '低' | '中' | '高'
export type SeveritySource = 'primary' | 'followup'

export interface VideoInfo {
  src: string
  fps: number
  width: number
  height: number
  total_frames: number
  duration_sec: number
}

/** 一个代表帧的完整信息（检测 + 多模态解读） */
export interface Representative {
  frame_index: number
  time_sec: number | null
  confidence: number
  /** YOLO 检测框 [x1, y1, x2, y2]，相对原视频分辨率 */
  bbox: [number, number, number, number] | null
  num_accident_boxes: number
  /** ROI 裁剪图相对路径，如 frames/roi/accident_frame_0083_roi.jpg */
  roi: string | null
  /** 完整帧相对路径，用于在监控画面上叠加检测框 */
  full: string | null
  severity: Severity | null
  severity_source: SeveritySource | null
  description: string | null
  ok: boolean
  /** 模型文中给过互相矛盾的等级 */
  ambiguous: boolean
  /** 模型输出有重复退化 */
  degenerate: boolean
  /** 是否为决定事件等级的那一帧 */
  is_lead: boolean
}

export interface AccidentEvent {
  event_id: number
  start_sec: number
  end_sec: number
  start_frame: number
  end_frame: number
  num_frames: number
  duration_sec: number
  peak_confidence: number
  /** 事件级等级 = 所辖代表帧的最高等级（详见 DEMO_PLAN.md 附录 B） */
  event_severity: Severity | null
  event_severity_frame: number | null
  /** 各代表帧等级序列，用于展示分歧 */
  event_severity_levels: Severity[]
  representatives: Representative[]
  /** 决定事件等级的那一帧（找不到则退回首个代表帧） */
  lead: Representative | null
}

export interface TimelinePoint {
  frame_index: number
  time_sec: number
  confidence: number
  bbox: [number, number, number, number] | null
  /** 属于哪个事件；不在任何事件区间内时为空 */
  event_id: number | null
}

export interface Stats {
  raw_accident_frames: number
  events: number
  frames_sent: number
  understood: number
  failed: number
  degenerate_outputs: number
  ambiguous_severity: number
  severity_from_followup: number
  /** 帧级分布，仅供参考 */
  severity_distribution: Record<string, number>
  /** 事件级分布，界面上的等级统计一律用这个 */
  event_severity_distribution: Record<string, number>
  event_severity_rule: string
}

export interface DemoData {
  generated_at: string
  title: { zh: string; en: string }
  video: VideoInfo
  severity_order: Record<string, number>
  stats: Stats
  source: {
    video_path: string
    weights: string
    conf_threshold: number
    temperature: number
    api_base: string
  }
  /** 已按 高 → 中 → 低 排好序，前端不要再自己比中文字符串 */
  events: AccidentEvent[]
  timeline: TimelinePoint[]
}
