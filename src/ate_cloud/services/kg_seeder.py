"""KGSeeder — seeds the Neo4j FMEA knowledge graph with electronics fault records.

Provides 100+ generic electronics fault records across 6 categories:
    1. Communication/Interconnects (通信/互连)
    2. Power (电源)
    3. Assembly/Soldering (装配/焊接)
    4. Passive Components (无源元件)
    5. Environmental/ESD (环境/静电)
    6. Mixed-Signal/Timing (混合信号/时序)

Each record contains: symptom (Chinese+English), cause, solution, affected
component, product type, error code, and diagnostic instrument. The seeder
uses MERGE (idempotent) Cypher statements so re-running is safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ate_cloud.services.neo4j_graph_service import Neo4jGraphService

logger = logging.getLogger(__name__)

# Cypher for seeding a single fault record (idempotent via MERGE).
_SEED_CYPHER = """
MERGE (s:FaultSymptom {name: $symptom_en})
  SET s.description_zh = $symptom_zh,
      s.description_en = $symptom_en,
      s.category = $category
MERGE (c:Cause {name: $cause_en})
  SET c.description_zh = $cause_zh,
      c.description_en = $cause_en
MERGE (sol:Solution {name: $solution_en})
  SET sol.description_zh = $solution_zh,
      sol.description_en = $solution_en
MERGE (comp:Component {name: $component})
  SET comp.type = $component_type
MERGE (prod:Product {name: $product_type})
  SET prod.type = $product_type
MERGE (err:ErrorCode {code: $error_code})
MERGE (inst:Instrument {name: $instrument})
MERGE (s)-[:HAS_CAUSE]->(c)
MERGE (c)-[:HAS_SOLUTION]->(sol)
MERGE (sol)-[:USES_INSTRUMENT]->(inst)
MERGE (s)-[:AFFECTS_COMPONENT]->(comp)
MERGE (s)-[:OCCURS_IN_PRODUCT]->(prod)
MERGE (s)-[:TRIGGERS_ERROR_CODE]->(err)
"""


@dataclass(frozen=True)
class FaultRecord:
    """A single FMEA fault record for knowledge graph seeding.

    Attributes:
        symptom_zh: Symptom description in Chinese.
        symptom_en: Symptom description in English.
        cause_zh: Root cause description in Chinese.
        cause_en: Root cause description in English.
        solution_zh: Solution/repair description in Chinese.
        solution_en: Solution/repair description in English.
        component: Affected component name.
        component_type: Component category (e.g. IC, connector, passive).
        product_type: Product type where symptom occurs.
        error_code: Error code triggered by the symptom.
        category: FMEA category (one of 6 categories).
        instrument: Diagnostic instrument name.
    """

    symptom_zh: str
    symptom_en: str
    cause_zh: str
    cause_en: str
    solution_zh: str
    solution_en: str
    component: str
    component_type: str
    product_type: str
    error_code: str
    category: str
    instrument: str


# ── Fault categories ─────────────────────────────────────────────────────

CAT_COMM = "communication_interconnects"
CAT_POWER = "power"
CAT_ASSEMBLY = "assembly_soldering"
CAT_PASSIVE = "passive_components"
CAT_ENVIRONMENT = "environmental_esd"
CAT_MIXED = "mixed_signal_timing"


def _build_fault_records() -> list[FaultRecord]:
    """Build the full list of 100+ electronics fault records.

    Returns:
        List of :class:`FaultRecord` across 6 FMEA categories.
    """
    records: list[FaultRecord] = []

    # ── Category 1: Communication/Interconnects (通信/互连) ──
    comm_faults = [
        ("I2C总线通信失败", "I2C bus communication failure",
         "上拉电阻值过大导致SCL/SDA上升时间不足", "Pull-up resistor too large causing insufficient SCL/SDA rise time",
         "更换为4.7kΩ上拉电阻，验证上升时间<1μs", "Replace with 4.7kΩ pull-up resistors, verify rise time <1μs",
         "I2C Bus", "Bus", "Communication Module", "I2C_TIMEOUT", "Oscilloscope"),
        ("I2C地址冲突", "I2C address collision",
         "两个从设备使用相同I2C地址", "Two slave devices share the same I2C address",
         "修改从设备地址或使用I2C多路复用器", "Modify slave address or use I2C multiplexer",
         "I2C Sensor", "Sensor", "Sensor Board", "I2C_ADDR_CONFLICT", "Logic Analyzer"),
        ("SPI时钟极性错误", "SPI clock polarity mismatch",
         "CPOL配置与从设备要求不一致", "CPOL configuration does not match slave requirement",
         "修改SPI控制器CPOL/CPHA设置", "Modify SPI controller CPOL/CPHA settings",
         "SPI Controller", "IC", "MCU Board", "SPI_MODE_ERR", "Oscilloscope"),
        ("SPI片选信号抖动", "SPI chip select signal glitch",
         "CS线路布线过长引入噪声", "CS trace too long introducing noise",
         "缩短CS走线或增加去耦电容", "Shorten CS trace or add decoupling capacitor",
         "SPI Bus", "Bus", "MCU Board", "SPI_CS_GLITCH", "Oscilloscope"),
        ("UART帧错误", "UART framing error",
         "波特率不匹配导致停止位检测失败", "Baud rate mismatch causing stop bit detection failure",
         "校准波特率，确保±2%容差内", "Calibrate baud rate, ensure within ±2% tolerance",
         "UART Transceiver", "IC", "Communication Module", "UART_FRAME_ERR", "Logic Analyzer"),
        ("UART奇偶校验失败", "UART parity check failure",
         "电磁干扰导致数据位翻转", "EMI causing data bit flips",
         "增加屏蔽措施，启用硬件流控", "Add shielding, enable hardware flow control",
         "UART Bus", "Bus", "Industrial Controller", "UART_PARITY_ERR", "Logic Analyzer"),
        ("CAN总线位错误", "CAN bus bit error",
         "终端电阻缺失导致信号反射", "Missing termination resistor causing signal reflection",
         "在总线两端安装120Ω终端电阻", "Install 120Ω termination resistors at both bus ends",
         "CAN Transceiver", "IC", "Automotive ECU", "CAN_BIT_ERR", "Oscilloscope"),
        ("CAN总线总线关闭", "CAN bus-off state",
         "错误计数器超过255阈值", "Error counter exceeded 255 threshold",
         "检查总线物理层，重启CAN控制器", "Check bus physical layer, restart CAN controller",
         "CAN Controller", "IC", "Automotive ECU", "CAN_BUS_OFF", "CAN Analyzer"),
        ("USB枚举失败", "USB enumeration failure",
         "D+/D-线路互换", "D+/D- lines swapped",
         "修正USB差分对布线", "Correct USB differential pair routing",
         "USB Port", "Connector", "Consumer Device", "USB_ENUM_FAIL", "USB Analyzer"),
        ("USB过流保护触发", "USB over-current protection triggered",
         "VBUS短路到地", "VBUS shorted to ground",
         "检查VBUS电源路径，更换损坏的TVS二极管", "Check VBUS power path, replace damaged TVS diode",
         "USB Power", "Protection", "Consumer Device", "USB_OVERCURRENT", "Digital Multimeter"),
        ("RS485通信中断", "RS485 communication interruption",
         "差分线不平衡导致共模电压超限", "Differential line imbalance causing common-mode voltage excursion",
         "匹配终端电阻，增加共模扼流圈", "Match termination resistors, add common-mode choke",
         "RS485 Transceiver", "IC", "Industrial Gateway", "RS485_COMM_LOST", "Oscilloscope"),
        ("RS232电平异常", "RS232 level abnormality",
         "MAX232电荷泵电容失效", "MAX232 charge pump capacitor failure",
         "更换1μF电荷泵电容", "Replace 1μF charge pump capacitor",
         "RS232 Transceiver", "IC", "Legacy Interface", "RS232_LEVEL_ERR", "Oscilloscope"),
        ("PCIe链路训练失败", "PCIe link training failure",
         "参考时钟缺失或不稳定", "Reference clock missing or unstable",
         "检查100MHz参考时钟信号完整性", "Check 100MHz reference clock signal integrity",
         "PCIe Slot", "Connector", "Server Board", "PCIE_LTSSM_FAIL", "Oscilloscope"),
        ("MIPI D-PHY同步丢失", "MIPI D-PHY sync loss",
         "高速时钟通道差分对不匹配", "HS clock channel differential pair mismatch",
         "调整差分对长度匹配<5mil", "Adjust differential pair length matching <5mil",
         "MIPI Interface", "Bus", "Camera Module", "MIPI_SYNC_LOSS", "Oscilloscope"),
        ("JTAG调试连接失败", "JTAG debug connection failure",
         "TCK/TMS信号交叉连接", "TCK/TMS signals cross-connected",
         "检查JTAG引脚映射，修正连接", "Check JTAG pin mapping, correct connections",
         "JTAG Header", "Connector", "MCU Board", "JTAG_NO_CONNECT", "Logic Analyzer"),
        ("以太网PHY链路断开", "Ethernet PHY link down",
         "RJ45磁耦变压器开路", "RJ45 magnetic transformer open circuit",
         "更换以太网磁耦变压器", "Replace Ethernet magnetic transformer",
         "Ethernet PHY", "IC", "Network Router", "ETH_LINK_DOWN", "Cable Tester"),
        ("蓝牙配对失败", "Bluetooth pairing failure",
         "天线匹配网络阻抗失配", "Antenna matching network impedance mismatch",
         "重新调试天线匹配网络", "Re-tune antenna matching network",
         "Bluetooth Module", "Module", "Wireless Device", "BT_PAIR_FAIL", "Spectrum Analyzer"),
        ("SD卡初始化超时", "SD card initialization timeout",
         "SD卡电源上电时序不正确", "SD card power-up sequence incorrect",
         "确保VDD先于CMD上电，等待250ms后发送CMD0", "Ensure VDD before CMD, wait 250ms before CMD0",
         "SD Card Slot", "Connector", "Embedded System", "SD_INIT_TIMEOUT", "Oscilloscope"),
    ]
    for sz, se, cz, ce, solz, sole, comp, ct, prod, ec, inst in comm_faults:
        records.append(FaultRecord(
            symptom_zh=sz, symptom_en=se, cause_zh=cz, cause_en=ce,
            solution_zh=solz, solution_en=sole, component=comp,
            component_type=ct, product_type=prod, error_code=ec,
            category=CAT_COMM, instrument=inst,
        ))

    # ── Category 2: Power (电源) ──
    power_faults = [
        ("3.3V电源轨超出容差", "3.3V power rail out of tolerance",
         "LDO稳压器输出电容ESR过高", "LDO regulator output capacitor ESR too high",
         "更换低ESR陶瓷电容(10μF X5R)", "Replace with low-ESR ceramic capacitor (10μF X5R)",
         "LDO Regulator", "IC", "Power Board", "PWR_33V_OOR", "Digital Multimeter"),
        ("5V电源轨纹波过大", "5V power rail excessive ripple",
         "开关电源输出滤波电容容量衰减", "Switching power supply output filter capacitor degraded",
         "更换输出滤波电容(100μF低ESR)", "Replace output filter capacitor (100μF low-ESR)",
         "Buck Converter", "IC", "Power Board", "PWR_5V_RIPPLE", "Oscilloscope"),
        ("12V电源轨过压保护", "12V power rail over-voltage protection",
         "反馈分压电阻漂移", "Feedback divider resistor drift",
         "更换0.1%精度反馈电阻", "Replace 0.1% precision feedback resistors",
         "Buck Converter", "IC", "Industrial Power", "PWR_12V_OVP", "Digital Multimeter"),
        ("电池电压检测异常", "Battery voltage detection abnormality",
         "ADC参考电压不准", "ADC reference voltage inaccurate",
         "校准ADC参考电压或更换基准源", "Calibrate ADC reference or replace voltage reference",
         "Battery Monitor", "IC", "Portable Device", "BAT_VOLT_ERR", "Digital Multimeter"),
        ("上电浪涌电流过大", "Power-on inrush current excessive",
         "输入电容容量过大无软启动", "Input capacitance too large without soft-start",
         "增加NTC热敏电阻或软启动电路", "Add NTC thermistor or soft-start circuit",
         "Input Filter", "Passive", "Power Board", "PWR_INRUSH", "Current Probe"),
        ("电源排序错误", "Power sequencing error",
         "电源管理IC时序配置不正确", "Power management IC timing misconfiguration",
         "重新配置PMIC时序寄存器", "Reconfigure PMIC timing registers",
         "PMIC", "IC", "Server Board", "PWR_SEQ_ERR", "Oscilloscope"),
        ("DC-DC转换器效率下降", "DC-DC converter efficiency drop",
         "电感饱和电流不足", "Inductor saturation current insufficient",
         "更换更大饱和电流的电感", "Replace inductor with higher saturation current",
         "Buck Converter", "IC", "Power Board", "PWR_EFF_LOW", "Power Analyzer"),
        ("PDN阻抗不匹配", "PDN impedance mismatch",
         "去耦电容位置不合理", "Decoupling capacitor placement suboptimal",
         "优化去耦电容布局，靠近IC电源引脚", "Optimize decoupling capacitor layout near IC power pins",
         "Power Distribution", "Passive", "Server Board", "PDN_Z_MISMATCH", "Vector Network Analyzer"),
        ("热关断保护触发", "Thermal shutdown triggered",
         "稳压器散热不足结温过高", "Regulator insufficient heatsinking, junction over-temperature",
         "增加散热铜面积或加装散热片", "Increase thermal copper area or add heatsink",
         "LDO Regulator", "IC", "Industrial Controller", "PWR_TSD", "Thermal Camera"),
        ("电源使能信号浮动", "Power enable signal floating",
         "EN引脚无上拉电阻", "EN pin missing pull-up resistor",
         "增加100kΩ上拉电阻到VIN", "Add 100kΩ pull-up resistor to VIN",
         "Buck Converter", "IC", "MCU Board", "PWR_EN_FLOAT", "Oscilloscope"),
        ("负压轨无输出", "Negative voltage rail no output",
         "反相电荷泵二极管正向压降过大", "Inverting charge pump diode forward voltage drop too high",
         "更换肖特基二极管降低正向压降", "Replace Schottky diode to reduce forward voltage",
         "Charge Pump", "IC", "Analog Front End", "PWR_NEG_RAIL", "Digital Multimeter"),
        ("负载瞬态响应差", "Poor load transient response",
         "补偿网络参数不合理", "Compensation network parameters suboptimal",
         "重新设计Type II/III补偿网络", "Redesign Type II/III compensation network",
         "Buck Converter", "IC", "Server Board", "PWR_TRANS_POOR", "Oscilloscope"),
        ("电源轨短路", "Power rail short circuit",
         "PCB内层电源/地平面短路", "PCB inner layer power/ground plane short",
         "使用热成像定位短路点，切割短路铜箔", "Locate short with thermal imaging, cut shorted copper",
         "Power Plane", "PCB", "Server Board", "PWR_SHORT", "Thermal Camera"),
        ("VRM过流保护", "VRM over-current protection",
         "负载电流超过VRM额定值", "Load current exceeds VRM rating",
         "升级VRM或分流负载到多相", "Upgrade VRM or distribute load across phases",
         "VRM", "IC", "Server Board", "VRM_OCP", "Current Probe"),
        ("电池充电异常", "Battery charging abnormality",
         "充电IC温度补偿失效", "Charge IC temperature compensation failure",
         "更换充电IC，检查NTC热敏电阻", "Replace charge IC, check NTC thermistor",
         "Charge Controller", "IC", "Portable Device", "BAT_CHG_ERR", "Digital Multimeter"),
        ("电源毛刺导致复位", "Power glitch causing system reset",
         "复位IC阈值设置不当", "Reset IC threshold set incorrectly",
         "调整复位IC阈值或增加延迟", "Adjust reset IC threshold or add delay",
         "Reset IC", "IC", "MCU Board", "PWR_GLITCH_RST", "Oscilloscope"),
        ("LDO dropout电压不足", "LDO dropout voltage insufficient",
         "输入输出压差低于dropout规格", "Input-output voltage difference below dropout spec",
         "降低负载电流或更换更低dropout的LDO", "Reduce load current or use lower-dropout LDO",
         "LDO Regulator", "IC", "Sensor Board", "LDO_DROPOUT", "Digital Multimeter"),
        ("多路电源交叉干扰", "Multiple power rail cross-interference",
         "电源平面分割不合理导致串扰", "Poor power plane splitting causing crosstalk",
         "重新规划电源平面分割，增加隔离带", "Re-plan power plane splitting, add isolation gaps",
         "Power Plane", "PCB", "Server Board", "PWR_CROSSTALK", "Oscilloscope"),
    ]
    for sz, se, cz, ce, solz, sole, comp, ct, prod, ec, inst in power_faults:
        records.append(FaultRecord(
            symptom_zh=sz, symptom_en=se, cause_zh=cz, cause_en=ce,
            solution_zh=solz, solution_en=sole, component=comp,
            component_type=ct, product_type=prod, error_code=ec,
            category=CAT_POWER, instrument=inst,
        ))

    # ── Category 3: Assembly/Soldering (装配/焊接) ──
    assembly_faults = [
        ("BGA焊球断裂", "BGA solder ball fracture",
         "PCB弯曲应力导致BGA焊球开裂", "PCB bending stress causing BGA solder ball cracking",
         "增加PCB支撑，使用底部填充胶", "Add PCB support, use underfill adhesive",
         "BGA Package", "IC", "Server Board", "SOL_BGA_CRACK", "X-Ray Inspector"),
        ("冷焊点", "Cold solder joint",
         "焊接温度不足导致焊锡未完全润湿", "Insufficient soldering temperature causing incomplete wetting",
         "返修焊点，提高烙铁温度至350°C", "Rework joint, increase iron temperature to 350°C",
         "Solder Joint", "Solder", "MCU Board", "SOL_COLD_JOINT", "Visual Inspector"),
        ("焊桥短路", "Solder bridge short",
         "焊锡膏印刷量过多", "Excessive solder paste deposition",
         "减小钢网开口或降低刮刀压力", "Reduce stencil aperture or lower squeegee pressure",
         "Solder Joint", "Solder", "MCU Board", "SOL_BRIDGE", "Visual Inspector"),
        ("虚焊", "Dry joint (insufficient solder)",
         "焊锡量不足导致连接不可靠", "Insufficient solder causing unreliable connection",
         "返修焊点，补充适量焊锡", "Rework joint, add appropriate solder amount",
         "Solder Joint", "Solder", "Power Board", "SOL_DRY_JOINT", "Visual Inspector"),
        ("PCB内层断路", "PCB inner layer open circuit",
         "PCB制造缺陷导致内层铜箔断裂", "PCB manufacturing defect causing inner copper trace break",
         "飞线修复或更换PCB", "Wire jumper repair or replace PCB",
         "PCB Trace", "PCB", "MCU Board", "PCB_INNER_OPEN", "Multimeter Continuity"),
        ("过孔不通", "Via open circuit",
         "过孔电镀工艺不良导致孔壁铜层不连续", "Poor via plating causing discontinuous copper barrel",
         "钻孔重新电镀或更换PCB", "Drill and replate via or replace PCB",
         "Via", "PCB", "MCU Board", "PCB_VIA_OPEN", "Multimeter Continuity"),
        ("元件立碑", "Tombstoning (chip component standing)",
         "回流焊时两端润湿力不均", "Uneven wetting force at both ends during reflow",
         "优化回流焊温度曲线，调整焊盘尺寸", "Optimize reflow profile, adjust pad dimensions",
         "Chip Component", "Passive", "MCU Board", "SOL_TOMBSTONE", "Visual Inspector"),
        ("QFN底部散热焊盘虚焊", "QFN thermal pad poor soldering",
         "散热焊盘焊锡膏面积不足", "Thermal pad solder paste area insufficient",
         "增加焊锡膏印刷面积比例至70%", "Increase solder paste coverage to 70%",
         "QFN Package", "IC", "Power Board", "SOL_QFN_THERMAL", "X-Ray Inspector"),
        ("焊盘起翘", "Pad lifting",
         "焊接或返修温度过高导致焊盘与基材分离", "Excessive soldering/rework temperature causing pad delamination",
         "降低返修温度，飞线修复", "Lower rework temperature, wire jumper repair",
         "PCB Pad", "PCB", "MCU Board", "PCB_PAD_LIFT", "Visual Inspector"),
        ("锡须生长", "Tin whisker growth",
         "纯锡镀层在应力下产生锡须", "Pure tin plating growing whiskers under stress",
         "使用含锡铋合金镀层或添加镍阻挡层", "Use Sn-Bi alloy plating or add nickel barrier layer",
         "Plating", "Finish", "Legacy Board", "SOL_WHISKER", "SEM Inspector"),
        ("元件偏移", "Component misalignment",
         "贴片机精度不足或焊盘设计不合理", "Pick-place accuracy insufficient or pad design suboptimal",
         "校准贴片机，优化焊盘尺寸对称性", "Calibrate pick-place, optimize pad symmetry",
         "Chip Component", "Passive", "MCU Board", "SOL_OFFSET", "Visual Inspector"),
        ("通孔元件焊锡不足", "Through-hole insufficient solder",
         "波峰焊焊接角度或速度不当", "Wave soldering angle or speed incorrect",
         "调整波峰焊参数，增加焊接时间", "Adjust wave soldering parameters, increase dwell time",
         "Through-hole Joint", "Solder", "Power Board", "SOL_TH_INSUFF", "Visual Inspector"),
        ("阻焊层起泡", "Solder mask blistering",
         "PCB清洗不彻底残留助焊剂", "Incomplete PCB cleaning leaving flux residue",
         "增加清洗工序，使用兼容性助焊剂", "Add cleaning step, use compatible flux",
         "Solder Mask", "PCB", "MCU Board", "PCB_MASK_BLIST", "Visual Inspector"),
        ("元件极性反向", "Component polarity reversed",
         "贴片机方向识别错误", "Pick-place orientation detection error",
         "校准贴片机视觉系统，检查BOM极性标注", "Calibrate vision system, check BOM polarity marking",
         "Electrolytic Capacitor", "Passive", "Power Board", "SOL_POLARITY", "Visual Inspector"),
        ("连接器插拔力过大", "Connector excessive insertion force",
         "连接器引脚变形或对准不良", "Connector pin deformation or poor alignment",
         "更换损坏连接器，检查PCB安装孔位", "Replace damaged connector, check PCB mounting holes",
         "Board Connector", "Connector", "Industrial Controller", "SOL_CONN_FORCE", "Force Gauge"),
        ("底部填充空洞", "Underfill voiding",
         "底部填充工艺参数不当", "Underfill process parameters incorrect",
         "优化点胶路径和固化温度曲线", "Optimize dispense path and cure temperature profile",
         "Underfill", "Material", "Mobile Device", "SOL_UF_VOID", "X-Ray Inspector"),
        ("钢网堵塞导致少锡", "Stencil blockage causing insufficient solder",
         "钢网开口残留焊锡膏干涸", "Stencil aperture dried solder paste residue",
         "增加钢网清洗频率，使用底部擦拭", "Increase stencil cleaning frequency, add underside wipe",
         "Solder Joint", "Solder", "MCU Board", "SOL_STENCIL_BLK", "Visual Inspector"),
    ]
    for sz, se, cz, ce, solz, sole, comp, ct, prod, ec, inst in assembly_faults:
        records.append(FaultRecord(
            symptom_zh=sz, symptom_en=se, cause_zh=cz, cause_en=ce,
            solution_zh=solz, solution_en=sole, component=comp,
            component_type=ct, product_type=prod, error_code=ec,
            category=CAT_ASSEMBLY, instrument=inst,
        ))

    # ── Category 4: Passive Components (无源元件) ──
    passive_faults = [
        ("电容容量衰减", "Capacitor capacitance decay",
         "电解电容电解液干涸", "Electrolytic capacitor electrolyte dry-out",
         "更换电解电容，选用固态电容替代", "Replace electrolytic capacitor, use solid-state alternative",
         "Electrolytic Capacitor", "Passive", "Power Board", "CAP_DECAY", "LCR Meter"),
        ("电容短路", "Capacitor short circuit",
         "陶瓷电容机械应力导致内部裂纹", "Ceramic capacitor mechanical stress causing internal crack",
         "更换电容，减少PCB机械应力", "Replace capacitor, reduce PCB mechanical stress",
         "Ceramic Capacitor", "Passive", "MCU Board", "CAP_SHORT", "LCR Meter"),
        ("电容ESR增大", "Capacitor ESR increase",
         "电容老化导致等效串联电阻增大", "Capacitor aging increasing equivalent series resistance",
         "更换低ESR电容", "Replace with low-ESR capacitor",
         "Electrolytic Capacitor", "Passive", "Power Board", "CAP_ESR_HIGH", "ESR Meter"),
        ("电阻阻值漂移", "Resistor value drift",
         "厚膜电阻在高温下老化", "Thick film resistor aging at high temperature",
         "更换0.1%精度薄膜电阻", "Replace with 0.1% precision thin-film resistor",
         "Resistor", "Passive", "Sensor Board", "RES_DRIFT", "Digital Multimeter"),
        ("电阻开路", "Resistor open circuit",
         "浪涌电流烧毁电阻", "Surge current burning out resistor",
         "更换大功率电阻，增加TVS保护", "Replace with higher power resistor, add TVS protection",
         "Resistor", "Passive", "Power Board", "RES_OPEN", "Digital Multimeter"),
        ("电感饱和", "Inductor saturation",
         "工作电流超过电感饱和电流额定值", "Operating current exceeds inductor saturation current rating",
         "更换更大饱和电流的电感", "Replace with inductor of higher saturation current",
         "Inductor", "Passive", "Power Board", "IND_SAT", "LCR Meter"),
        ("电感Q值下降", "Inductor Q factor drop",
         "电感磁芯材料老化", "Inductor core material aging",
         "更换高频磁芯电感", "Replace with high-frequency core inductor",
         "Inductor", "Passive", "RF Module", "IND_Q_LOW", "LCR Meter"),
        ("磁珠失效", "Ferrite bead failure",
         "过电流导致磁珠饱和失去抑制能力", "Overcurrent causing ferrite bead saturation losing suppression",
         "更换更大额定电流的磁珠", "Replace with higher rated current ferrite bead",
         "Ferrite Bead", "Passive", "MCU Board", "FB_FAIL", "LCR Meter"),
        ("热敏电阻精度偏差", "Thermistor accuracy deviation",
         "NTC热敏电阻老化导致阻值漂移", "NTC thermistor aging causing resistance drift",
         "更换NTC热敏电阻，重新校准温度曲线", "Replace NTC thermistor, recalibrate temperature curve",
         "NTC Thermistor", "Passive", "Battery Pack", "NTC_DRIFT", "Digital Multimeter"),
        ("压敏电阻击穿", "Varistor breakdown",
         "过压事件导致MOV性能退化", "Overvoltage event causing MOV degradation",
         "更换压敏电阻，检查浪涌保护方案", "Replace varistor, review surge protection scheme",
         "Varistor", "Passive", "Power Board", "MOV_BREAKDOWN", "Hipot Tester"),
        ("晶体振荡器频率偏移", "Crystal oscillator frequency drift",
         "负载电容不匹配导致频率偏移", "Load capacitor mismatch causing frequency offset",
         "调整负载电容值，校准频率", "Adjust load capacitor value, calibrate frequency",
         "Crystal Oscillator", "Passive", "MCU Board", "XTAL_FREQ_DRIFT", "Frequency Counter"),
        ("晶体振荡器不起振", "Crystal oscillator not starting",
         "振荡电路负阻不足", "Oscillator circuit negative resistance insufficient",
         "减小负载电容或更换低ESR晶体", "Reduce load capacitor or use lower-ESR crystal",
         "Crystal Oscillator", "Passive", "MCU Board", "XTAL_NO_START", "Oscilloscope"),
        ("陶瓷电容压电效应", "Ceramic capacitor piezoelectric effect",
         "MLCC在电压作用下产生机械振动噪声", "MLCC generating acoustic noise under voltage",
         "更换为Class I介质或聚合物电容", "Replace with Class I dielectric or polymer capacitor",
         "Ceramic Capacitor", "Passive", "Audio Module", "CAP_PIEZO", "Oscilloscope"),
        ("去耦电容谐振", "Decoupling capacitor resonance",
         "去耦电容与PCB寄生电感形成LC谐振", "Decoupling capacitor and PCB parasitic inductance forming LC resonance",
         "增加不同容值电容并联展宽频带", "Add parallel capacitors of different values to broaden bandwidth",
         "Decoupling Capacitor", "Passive", "Server Board", "CAP_RESONANCE", "Vector Network Analyzer"),
        ("保险丝熔断", "Fuse blown",
         "过流事件导致保险丝熔断", "Overcurrent event blowing fuse",
         "更换同规格保险丝，排查过流原因", "Replace with same spec fuse, investigate overcurrent cause",
         "Fuse", "Protection", "Power Board", "FUSE_BLOWN", "Digital Multimeter"),
        ("光耦老化", "Optocoupler aging",
         "LED发光效率下降导致CTR降低", "LED luminous efficiency decline reducing CTR",
         "更换光耦，重新计算CTR裕量", "Replace optocoupler, recalculate CTR margin",
         "Optocoupler", "IC", "Isolation Module", "OPTO_CTR_LOW", "Digital Multimeter"),
        ("继电器触点氧化", "Relay contact oxidation",
         "触点弧光放电导致氧化", "Contact arcing causing oxidation",
         "更换继电器，增加灭弧电路", "Replace relay, add arc suppression circuit",
         "Relay", "Electromechanical", "Industrial Controller", "RLY_OXIDATION", "Digital Multimeter"),
    ]
    for sz, se, cz, ce, solz, sole, comp, ct, prod, ec, inst in passive_faults:
        records.append(FaultRecord(
            symptom_zh=sz, symptom_en=se, cause_zh=cz, cause_en=ce,
            solution_zh=solz, solution_en=sole, component=comp,
            component_type=ct, product_type=prod, error_code=ec,
            category=CAT_PASSIVE, instrument=inst,
        ))

    # ── Category 5: Environmental/ESD (环境/静电) ──
    env_faults = [
        ("ESD损坏输入引脚", "ESD damage to input pin",
         "人体静电放电击穿IC输入保护二极管", "Human ESD damaging IC input protection diode",
         "增加TVS二极管，加强ESD防护", "Add TVS diode, strengthen ESD protection",
         "Input IC", "IC", "Portable Device", "ESD_INPUT_DAMAGE", "ESD Simulator"),
        ("ESD导致MCU复位", "ESD causing MCU reset",
         "ESD耦合到复位线路", "ESD coupling into reset line",
         "增加复位线滤波电容和ESD保护", "Add reset line filter capacitor and ESD protection",
         "MCU", "IC", "MCU Board", "ESD_MCU_RESET", "ESD Simulator"),
        ("ESD损坏通信接口", "ESD damage to communication interface",
         "RS485/CAN收发器ESD耐受不足", "RS485/CAN transceiver insufficient ESD tolerance",
         "更换高ESD耐受收发器(±15kV)", "Replace with high-ESS-tolerant transceiver (±15kV)",
         "CAN Transceiver", "IC", "Industrial Gateway", "ESD_COMM_DAMAGE", "ESD Simulator"),
        ("高温导致时钟漂移", "High temperature causing clock drift",
         "TCXO温度补偿范围不足", "TCXO temperature compensation range insufficient",
         "更换更宽温度范围的TCXO或OCXO", "Replace with wider temperature range TCXO or OCXO",
         "TCXO", "Passive", "Communication Module", "TEMP_CLK_DRIFT", "Frequency Counter"),
        ("低温启动失败", "Low temperature startup failure",
         "电容容量在低温下急剧下降", "Capacitor capacitance dropping sharply at low temperature",
         "更换低温特性好的电容(X7R/NP0)", "Replace with low-temperature stable capacitor (X7R/NP0)",
         "Ceramic Capacitor", "Passive", "Industrial Controller", "TEMP_COLD_START", "Thermal Chamber"),
        ("湿度导致漏电", "Humidity causing leakage current",
         "PCB表面湿度凝结导致绝缘电阻下降", "PCB surface condensation reducing insulation resistance",
         "涂覆三防漆，增加湿度密封", "Apply conformal coating, add humidity sealing",
         "PCB Surface", "PCB", "Outdoor Device", "HUMID_LEAKAGE", "Insulation Tester"),
        ("温度循环导致焊点疲劳", "Temperature cycling causing solder fatigue",
         "CTE不匹配导致焊点热机械疲劳", "CTE mismatch causing solder joint thermo-mechanical fatigue",
         "使用柔性焊锡或增加底部填充", "Use flexible solder or add underfill",
         "Solder Joint", "Solder", "Automotive ECU", "TEMP_CYCLE_FATIGUE", "X-Ray Inspector"),
        ("振动导致连接器松动", "Vibration causing connector loosening",
         "连接器无锁扣机构", "Connector lacking locking mechanism",
         "更换带锁扣连接器，增加机械固定", "Replace with locking connector, add mechanical retention",
         "Board Connector", "Connector", "Automotive ECU", "VIBR_CONN_LOOSE", "Vibration Tester"),
        ("盐雾腐蚀", "Salt spray corrosion",
         "金属触点暴露在盐雾环境中腐蚀", "Metal contacts exposed to salt spray corroding",
         "使用防腐涂层，密封外壳", "Apply anti-corrosion coating, seal enclosure",
         "Connector Contact", "Connector", "Outdoor Device", "SALT_CORROSION", "Salt Spray Chamber"),
        ("灰尘导致短路", "Dust causing short circuit",
         "导电灰尘积累在高阻抗节点", "Conductive dust accumulating on high-impedance nodes",
         "增加防尘密封，定期清洁", "Add dust seal, schedule regular cleaning",
         "PCB Surface", "PCB", "Industrial Controller", "DUST_SHORT", "Insulation Tester"),
        ("ESD损坏MOSFET栅极", "ESD damage to MOSFET gate",
         "MOSFET栅极氧化层被静电击穿", "MOSFET gate oxide punctured by ESD",
         "增加栅极电阻和TVS保护", "Add gate resistor and TVS protection",
         "MOSFET", "IC", "Power Board", "ESD_GATE_DAMAGE", "ESD Simulator"),
        ("热失控", "Thermal runaway",
         "散热设计不足导致器件结温持续升高", "Insufficient cooling causing junction temperature rise",
         "改进散热设计，增加温度监控和关断", "Improve cooling, add temperature monitoring and shutdown",
         "Power MOSFET", "IC", "Power Board", "THERMAL_RUNAWAY", "Thermal Camera"),
        ("紫外线导致材料老化", "UV causing material degradation",
         "塑料外壳紫外线照射变脆开裂", "Plastic enclosure becoming brittle from UV exposure",
         "使用抗UV材料或增加UV涂层", "Use UV-resistant material or add UV coating",
         "Enclosure", "Mechanical", "Outdoor Device", "UV_DEGRADATION", "Visual Inspector"),
        ("电磁干扰导致ADC噪声", "EMI causing ADC noise",
         "开关电源辐射耦合到ADC输入", "Switching power supply radiation coupling to ADC input",
         "增加屏蔽和滤波，优化布局", "Add shielding and filtering, optimize layout",
         "ADC", "IC", "Sensor Board", "EMI_ADC_NOISE", "Spectrum Analyzer"),
        ("凝露导致腐蚀", "Condensation causing corrosion",
         "温度交变产生凝露水膜", "Temperature cycling creating condensation film",
         "增加呼吸器或密封干燥剂", "Add breather or seal with desiccant",
         "PCB Surface", "PCB", "Outdoor Device", "COND_CORROSION", "Insulation Tester"),
        ("海拔变化导致气压差", "Altitude change causing pressure differential",
         "密封内外压差导致连接器受力", "Pressure differential stressing connectors",
         "增加压力平衡呼吸器", "Add pressure-equalizing breather",
         "Enclosure", "Mechanical", "Avionics", "ALT_PRESSURE", "Pressure Gauge"),
        ("静电导致数据损坏", "ESD causing data corruption",
         "ESD事件导致存储器位翻转", "ESD event causing memory bit flips",
         "增加ESD保护，启用ECC纠错", "Add ESD protection, enable ECC correction",
         "Flash Memory", "IC", "MCU Board", "ESD_DATA_CORRUPT", "ESD Simulator"),
    ]
    for sz, se, cz, ce, solz, sole, comp, ct, prod, ec, inst in env_faults:
        records.append(FaultRecord(
            symptom_zh=sz, symptom_en=se, cause_zh=cz, cause_en=ce,
            solution_zh=solz, solution_en=sole, component=comp,
            component_type=ct, product_type=prod, error_code=ec,
            category=CAT_ENVIRONMENT, instrument=inst,
        ))

    # ── Category 6: Mixed-Signal/Timing (混合信号/时序) ──
    mixed_faults = [
        ("ADC偏移误差", "ADC offset error",
         "ADC内部基准电压偏移", "ADC internal reference voltage offset",
         "执行ADC自校准或外部校准", "Perform ADC self-calibration or external calibration",
         "ADC", "IC", "Sensor Board", "ADC_OFFSET", "Precision Voltage Source"),
        ("ADC增益误差", "ADC gain error",
         "ADC增益校准系数不正确", "ADC gain calibration coefficient incorrect",
         "重新校准ADC增益参数", "Recalibrate ADC gain parameters",
         "ADC", "IC", "Sensor Board", "ADC_GAIN", "Precision Voltage Source"),
        ("ADC漂移", "ADC drift",
         "温度变化导致ADC基准源漂移", "Temperature change causing ADC reference drift",
         "使用外部精密基准源，增加温度补偿", "Use external precision reference, add temperature compensation",
         "ADC", "IC", "Industrial Controller", "ADC_DRIFT", "Precision Voltage Source"),
        ("DAC输出非线性", "DAC output nonlinearity",
         "DAC单调性误差", "DAC monotonicity error",
         "执行DAC线性化校准查表", "Perform DAC linearization calibration lookup",
         "DAC", "IC", "Analog Output Module", "DAC_NONLINEAR", "Precision Multimeter"),
        ("时钟抖动过大", "Excessive clock jitter",
         "PLL环路滤波器参数不合理", "PLL loop filter parameters suboptimal",
         "重新设计PLL环路滤波器", "Redesign PLL loop filter",
         "PLL", "IC", "Communication Module", "CLK_JITTER", "Phase Noise Analyzer"),
        ("时钟相位噪声高", "High clock phase noise",
         "参考时钟信号质量差", "Reference clock signal quality poor",
         "使用更高品质因数的晶振", "Use higher quality factor crystal oscillator",
         "Clock Generator", "IC", "Communication Module", "CLK_PHASE_NOISE", "Phase Noise Analyzer"),
        ("建立时间不足", "Insufficient settling time",
         "ADC采样率过高超出建立时间", "ADC sampling rate too high exceeding settling time",
         "降低采样率或增加RC前端带宽", "Reduce sampling rate or increase RC front-end bandwidth",
         "ADC", "IC", "Data Acquisition", "ADC_SETTLE", "Oscilloscope"),
        ("串扰导致信号失真", "Crosstalk causing signal distortion",
         "相邻走线电磁耦合", "Electromagnetic coupling between adjacent traces",
         "增加走线间距或增加地线隔离", "Increase trace spacing or add ground isolation",
         "PCB Trace", "PCB", "High-speed Board", "SIG_CROSSTALK", "Oscilloscope"),
        ("DDR时序违反", "DDR timing violation",
         "DDR3/4信号建立/保持时间不足", "DDR3/4 signal setup/hold time insufficient",
         "调整DDR控制器时序参数，优化走线长度匹配", "Adjust DDR controller timing, optimize trace length matching",
         "DDR Memory", "IC", "Server Board", "DDR_TIMING_VIOLATION", "Oscilloscope"),
        ("DDR终端电阻失效", "DDR termination resistor failure",
         "ODT配置不正确", "ODT configuration incorrect",
         "重新配置DDR控制器ODT设置", "Reconfigure DDR controller ODT settings",
         "DDR Memory", "IC", "Server Board", "DDR_ODT_ERR", "Oscilloscope"),
        ("DDR刷新率不足", "DDR refresh rate insufficient",
         "刷新周期超过电容保持时间", "Refresh period exceeding capacitor retention time",
         "提高刷新率，检查温度补偿刷新", "Increase refresh rate, check temperature-compensated refresh",
         "DDR Memory", "IC", "Server Board", "DDR_REFRESH_ERR", "Memory Tester"),
        ("PLL锁定失败", "PLL lock failure",
         "输入参考时钟频率超出锁定范围", "Input reference clock frequency outside lock range",
         "检查参考时钟频率，调整PLL分频比", "Check reference clock, adjust PLL divider ratio",
         "PLL", "IC", "MCU Board", "PLL_LOCK_FAIL", "Oscilloscope"),
        ("运放失调电压大", "Op-amp large offset voltage",
         "运放输入失调电压随温度漂移", "Op-amp input offset voltage drifting with temperature",
         "增加调零电路或使用斩波稳零运放", "Add nulling circuit or use chopper-stabilized op-amp",
         "Op-amp", "IC", "Analog Front End", "OPAMP_OFFSET", "Digital Multimeter"),
        ("运放自激振荡", "Op-amp self-oscillation",
         "反馈环路相位裕度不足", "Feedback loop phase margin insufficient",
         "增加补偿电容提高相位裕度", "Add compensation capacitor to increase phase margin",
         "Op-amp", "IC", "Analog Front End", "OPAMP_OSC", "Oscilloscope"),
        ("比较器抖动", "Comparator chatter",
         "输入信号缓慢变化导致比较器在阈值附近振荡", "Slow input signal causing comparator oscillation near threshold",
         "增加迟滞电路", "Add hysteresis circuit",
         "Comparator", "IC", "Sensor Board", "CMP_CHATTER", "Oscilloscope"),
        ("采样混叠", "Sampling aliasing",
         "ADC采样率低于信号奈奎斯特频率", "ADC sampling rate below signal Nyquist frequency",
         "增加抗混叠滤波器或提高采样率", "Add anti-aliasing filter or increase sampling rate",
         "ADC", "IC", "Data Acquisition", "ADC_ALIASING", "Spectrum Analyzer"),
        ("LVDS眼图闭合", "LVDS eye diagram closed",
         "差分对长度不匹配导致码间干扰", "Differential pair length mismatch causing ISI",
         "匹配差分对长度<10mil，增加预加重", "Match differential pair length <10mil, add pre-emphasis",
         "LVDS Interface", "Bus", "Display Module", "LVDS_EYE_CLOSED", "Oscilloscope"),
    ]
    for sz, se, cz, ce, solz, sole, comp, ct, prod, ec, inst in mixed_faults:
        records.append(FaultRecord(
            symptom_zh=sz, symptom_en=se, cause_zh=cz, cause_en=ce,
            solution_zh=solz, solution_en=sole, component=comp,
            component_type=ct, product_type=prod, error_code=ec,
            category=CAT_MIXED, instrument=inst,
        ))

    return records


# Pre-built list of all fault records (generated once at import).
_FAULT_RECORDS: list[FaultRecord] = _build_fault_records()


class KGSeeder:
    """Seeds the Neo4j FMEA knowledge graph with electronics fault records.

    Provides 100+ fault records across 6 FMEA categories. Uses idempotent
    MERGE Cypher statements so re-seeding is safe.

    Args:
        graph_service: The :class:`Neo4jGraphService` to execute Cypher.
    """

    def __init__(self, graph_service: Neo4jGraphService) -> None:
        self._graph = graph_service

    @property
    def records(self) -> list[FaultRecord]:
        """All fault records available for seeding."""
        return list(_FAULT_RECORDS)

    @property
    def record_count(self) -> int:
        """Total number of fault records available."""
        return len(_FAULT_RECORDS)

    async def seed_all(self) -> dict[str, int]:
        """Seed all fault records into the Neo4j knowledge graph.

        Creates uniqueness constraints first, then MERGEs all fault
        records (nodes + relationships). Idempotent — re-running
        updates existing nodes without creating duplicates.

        Returns:
            Dict with ``nodes_created`` and ``relationships_created``
            (total counts in the graph after seeding).

        Raises:
            CircuitBreakerOpenError: If the Neo4j circuit is OPEN.
            Exception: Any Neo4j error during seeding.
        """
        # Create constraints first for MERGE performance.
        await self._graph.create_constraints()

        # Seed all fault records.
        for record in _FAULT_RECORDS:
            params: dict[str, Any] = {
                "symptom_zh": record.symptom_zh,
                "symptom_en": record.symptom_en,
                "cause_zh": record.cause_zh,
                "cause_en": record.cause_en,
                "solution_zh": record.solution_zh,
                "solution_en": record.solution_en,
                "component": record.component,
                "component_type": record.component_type,
                "product_type": record.product_type,
                "error_code": record.error_code,
                "category": record.category,
                "instrument": record.instrument,
            }
            await self._graph.write(_SEED_CYPHER, params)

        logger.info("Seeded %d fault records into Neo4j FMEA graph", len(_FAULT_RECORDS))

        # Count total nodes and relationships in the graph.
        nodes = await self._graph.count_nodes()
        rels = await self._graph.count_relationships()
        return {"nodes_created": nodes, "relationships_created": rels}


__all__ = ["KGSeeder", "FaultRecord"]
