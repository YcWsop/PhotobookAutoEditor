"""给 AI（或任何别的审核者）留的接口。默认什么都不做，所以默认没有任何东西离开这台机器。

auto.py 会调用下面三个函数；把它们换成你自己的实现即可（也可以另写一个模块，再改 auto.py 里的 import）。

1. review(queue) -> [(左页名, 右页名), ...]
   queue 里是规则判不了的相邻页对，每项是 dict：
     kind        'uncertain'（接缝规则差一点通过）或 'flat'（接缝是纯色，两边颜色几乎一样）
     left, right 页名（规则猜的左右顺序）
     left_img, right_img   两页图片的路径（书的 .work 文件夹里）
     stats       接缝数字（seam、ratio、jump、std）
   返回“确实是一张图、应当合并”的页对；没返回的保持分开，仍留在联系表里等人看。

2. gaps(queue, pages) -> [{'left': 页名, 'right': 页名, 'gap': 缝宽占一页宽的比例}, ...]
   两页本来是一张图，但中间缺了一条（老写真）。pages 是整本书按顺序的页面图片路径，
   AI 可以自己翻相邻页判断哪里缺了一条、缺多宽；也可以只看 queue。返回需要补缝的页对。

3. fill_gap(job) -> 补好的图片路径，或 None（不补）
   job 是 gap_repair.make_job() 生成的任务单（dict）：
     canvas   带透明通道的大图：左页 | 全透明的缝 | 右页（PNG，RGBA）
     mask     同尺寸蒙版，白色 = 要补的区域
     gap_box  [x0, y0, x1, y1] 缝在大图里的位置（像素）
     prompt   建议给模型的指令
   把 canvas（可连同 mask）交给有多模态/图像编辑能力的模型，只补透明的那一条，把结果存成图片后返回路径。
   补好的图尺寸可以不同，gap_repair.compose 会缩放回来，并且只取缝里的部分，两页原有像素不变。
   返回 None 时任务单留在 <书名>.gaps/ 里，书保持两页，之后把 <任务名>.filled.png 放在任务旁边、
   加 --force 重跑一次也能合进去。

隐私：这个仓库里永远不要出现 API key、token、账号名或个人路径。真正的实现只从【环境变量】读凭据
（变量设在仓库之外），不要把它们写进任何文件。
"""


def review(queue):
    return []


def gaps(queue, pages):
    return []


def fill_gap(job):
    return None
