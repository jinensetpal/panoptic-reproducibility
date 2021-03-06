from typing import Any, List, Mapping, Optional, Tuple
import numpy as np
import tensorflow as tf
import src.common as common

def get_metrics():
    metrics = [
        tf.keras.metrics.MeanIoU(common.NUM_CLASSES, 'IoU'),
        PanopticQuality(common.NUM_CLASSES, 0, common.MAX_INSTANCE_PER_CATEGORY, 0),
        PanopticInstanceAveragePrecision(common.NUM_CLASSES, [], common.PANOPTIC_LABEL_DIVISOR, 0)
    ]
    return metrics


def _ids_to_counts(id_array: np.ndarray) -> Mapping[int, int]:
    """Given a numpy array, a mapping from each unique entry to its count."""
    ids, counts = np.unique(id_array, return_counts=True)
    return dict(zip(ids, counts))


class PanopticInstanceAveragePrecision(tf.keras.metrics.Metric):
    """Computes instance segmentation AP of panoptic segmentations.

    Panoptic segmentation includes both "thing" and "stuff" classes. This class
    ignores the "stuff" classes to report metrics on only the "thing" classes
    that have discrete instances. It computes a series of AP-based metrics using
    the COCO evaluation scripts.
    """

    def __init__(self,
                 num_classes: int,
                 things_list: Collection[int],
                 label_divisor: int,
                 ignored_label: int,
                 name: str = 'panoptic_instance_ap',
                 **kwargs):
        """Constructs panoptic instance segmentation evaluation class."""
        super(PanopticInstanceAveragePrecision, self).__init__(name=name, **kwargs)
        self.num_classes = num_classes
        self.stuff_list = set(range(num_classes)).difference(things_list)
        self.label_divisor = label_divisor
        self.ignored_label = ignored_label
        self.detection_metric = InstanceAveragePrecision()
        self.reset_states()

    def reset_states(self) -> None:
        self.detection_metric.reset_states()

    def result(self) -> np.ndarray:
        return self.detection_metric.result()

    def update_state(self,
                     groundtruth_panoptic: tf.Tensor,
                     predicted_panoptic: tf.Tensor,
                     semantic_probability: tf.Tensor,
                     instance_score_map: tf.Tensor,
                     is_crowd_map: Optional[tf.Tensor] = None) -> None:
        """Adds the results from a new image to be computed by the metric.

        Args:
          groundtruth_panoptic: A 2D integer tensor, with the true panoptic label at
            each pixel.
          predicted_panoptic: 2D integer tensor with predicted panoptic labels to be
            evaluated.
          semantic_probability: An float tensor of shape `[image_height,
            image_width, num_classes]`. Specifies at each pixel the estimated
            probability distribution that that pixel belongs to each semantic class.
          instance_score_map: A 2D float tensor, where the pixels for an instance
            will have the probability of that being an instance.
          is_crowd_map: A 2D boolean tensor. Where it is True, the instance in that


class PanopticQuality(tf.keras.metrics.Metric):
    """Metric class for Panoptic Quality.

    "Panoptic Segmentation" by Alexander Kirillov, Kaiming He, Ross Girshick,
    Carsten Rother, Piotr Dollar.
    https://arxiv.org/abs/1801.00868

    Stand-alone usage:

    pq_obj = panoptic_quality.PanopticQuality(num_classes,
      max_instances_per_category, ignored_label)
    pq_obj.update_state(y_true_1, y_pred_1)
    pq_obj.update_state(y_true_2, y_pred_2)
    ...
    result = pq_obj.result().numpy()
    """

    def __init__(self,
                 num_classes: int,
                 ignored_label: int,
                 max_instances_per_category: int,
                 offset: int,
                 name: str = 'panoptic_quality',
                 **kwargs):
        """Initialization of the PanopticQuality metric.

        Args:
          num_classes: Number of classes in the dataset as an integer.
          ignored_label: The class id to be ignored in evaluation as an integer or
            integer tensor.
          max_instances_per_category: The maximum number of instances for each class
            as an integer or integer tensor.
          offset: The maximum number of unique labels as an integer or integer
            tensor.
          name: An optional variable_scope name. (default: 'panoptic_quality')
          **kwargs: The keyword arguments that are passed on to `fn`.
        """
        super(PanopticQuality, self).__init__(name=name, **kwargs)
        self.num_classes = num_classes
        self.ignored_label = ignored_label
        self.max_instances_per_category = max_instances_per_category
        self.total_iou = self.add_weight(
            'total_iou', shape=(num_classes,), initializer=tf.zeros_initializer)
        self.total_tp = self.add_weight(
            'total_tp', shape=(num_classes,), initializer=tf.zeros_initializer)
        self.total_fn = self.add_weight(
            'total_fn', shape=(num_classes,), initializer=tf.zeros_initializer)
        self.total_fp = self.add_weight(
            'total_fp', shape=(num_classes,), initializer=tf.zeros_initializer)
        self.offset = offset

    def compare_and_accumulate(
            self, gt_panoptic_label: tf.Tensor, pred_panoptic_label: tf.Tensor
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Compares predicted segmentation with groundtruth, accumulates its metric.

        It is not assumed that instance ids are unique across different categories.
        See for example combine_semantic_and_instance_predictions.py in official
        PanopticAPI evaluation code for issues to consider when fusing category
        and instance labels.

        Instances ids of the ignored category have the meaning that id 0 is "void"
        and remaining ones are crowd instances.

        Args:
          gt_panoptic_label: A tensor that combines label array from categories and
            instances for ground truth.
          pred_panoptic_label: A tensor that combines label array from categories
            and instances for the prediction.

        Returns:
          The value of the metrics (iou, tp, fn, fp) over all comparisons, as a
          float scalar.
        """
        iou_per_class = np.zeros(self.num_classes, dtype=np.float64)
        tp_per_class = np.zeros(self.num_classes, dtype=np.float64)
        fn_per_class = np.zeros(self.num_classes, dtype=np.float64)
        fp_per_class = np.zeros(self.num_classes, dtype=np.float64)

        # Pre-calculate areas for all groundtruth and predicted segments.
        gt_segment_areas = _ids_to_counts(gt_panoptic_label.numpy())
        pred_segment_areas = _ids_to_counts(pred_panoptic_label.numpy())

        # We assume the ignored segment has instance id = 0.
        ignored_panoptic_id = self.ignored_label * self.max_instances_per_category

        # Next, combine the groundtruth and predicted labels. Dividing up the pixels
        # based on which groundtruth segment and which predicted segment they belong
        # to, this will assign a different 64-bit integer label to each choice
        # of (groundtruth segment, predicted segment), encoded as
        #   gt_panoptic_label * offset + pred_panoptic_label.
        intersection_id_array = tf.cast(gt_panoptic_label,
                                        tf.int64) * self.offset + tf.cast(
            pred_panoptic_label, tf.int64)

        # For every combination of (groundtruth segment, predicted segment) with a
        # non-empty intersection, this counts the number of pixels in that
        # intersection.
        intersection_areas = _ids_to_counts(intersection_id_array.numpy())

        # Compute overall ignored overlap.
        def prediction_ignored_overlap(pred_panoptic_label):
            intersection_id = ignored_panoptic_id * self.offset + pred_panoptic_label
            return intersection_areas.get(intersection_id, 0)

        # Sets that are populated with which segments groundtruth/predicted segments
        # have been matched with overlapping predicted/groundtruth segments
        # respectively.
        gt_matched = set()
        pred_matched = set()

        # Calculate IoU per pair of intersecting segments of the same category.
        for intersection_id, intersection_area in intersection_areas.items():
            gt_panoptic_label = intersection_id // self.offset
            pred_panoptic_label = intersection_id % self.offset

            gt_category = gt_panoptic_label // self.max_instances_per_category
            pred_category = pred_panoptic_label // self.max_instances_per_category
            if gt_category != pred_category:
                continue
            if pred_category == self.ignored_label:
                continue

            # Union between the groundtruth and predicted segments being compared does
            # not include the portion of the predicted segment that consists of
            # groundtruth "void" pixels.
            union = (
                    gt_segment_areas[gt_panoptic_label] +
                    pred_segment_areas[pred_panoptic_label] - intersection_area -
                    prediction_ignored_overlap(pred_panoptic_label))
            iou = intersection_area / union
            if iou > 0.5:
                tp_per_class[gt_category] += 1
                iou_per_class[gt_category] += iou
                gt_matched.add(gt_panoptic_label)
                pred_matched.add(pred_panoptic_label)

        # Count false negatives for each category.
        for gt_panoptic_label in gt_segment_areas:
            if gt_panoptic_label in gt_matched:
                continue
            category = gt_panoptic_label // self.max_instances_per_category
            # Failing to detect a void segment is not a false negative.
            if category == self.ignored_label:
                continue
            fn_per_class[category] += 1

        # Count false positives for each category.
        for pred_panoptic_label in pred_segment_areas:
            if pred_panoptic_label in pred_matched:
                continue
            # A false positive is not penalized if is mostly ignored in the
            # groundtruth.
            if (prediction_ignored_overlap(pred_panoptic_label) /
                pred_segment_areas[pred_panoptic_label]) > 0.5:
                continue
            category = pred_panoptic_label // self.max_instances_per_category
            if category == self.ignored_label:
                continue
            fp_per_class[category] += 1
        return iou_per_class, tp_per_class, fn_per_class, fp_per_class

    def update_state(
            self,
            y_true: tf.Tensor,
            y_pred: tf.Tensor,
            sample_weight: Optional[tf.Tensor] = None) -> List[tf.Operation]:
        """Accumulates the panoptic quality statistics.

        Args:
          y_true: The ground truth panoptic label map (defined as semantic_map *
            max_instances_per_category + instance_map).
          y_pred: The predicted panoptic label map (defined as semantic_map *
            max_instances_per_category + instance_map).
          sample_weight: Optional weighting of each example. Defaults to 1. Can be a
            `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
            be broadcastable to `y_true`.

        Returns:
          Update ops for iou, tp, fn, fp.
        """
        result = self.compare_and_accumulate(y_true, y_pred)
        iou, tp, fn, fp = tuple(result)
        update_iou_op = self.total_iou.assign_add(iou)
        update_tp_op = self.total_tp.assign_add(tp)
        update_fn_op = self.total_fn.assign_add(fn)
        update_fp_op = self.total_fp.assign_add(fp)
        return [update_iou_op, update_tp_op, update_fn_op, update_fp_op]

    def result(self) -> tf.Tensor:
        """Computes the panoptic quality."""
        sq = tf.math.divide_no_nan(self.total_iou, self.total_tp)
        rq = tf.math.divide_no_nan(
            self.total_tp,
            self.total_tp + 0.5 * self.total_fn + 0.5 * self.total_fp)
        pq = tf.math.multiply(sq, rq)

        # Find the valid classes that will be used for evaluation. We will
        # ignore classes which have (tp + fn + fp) equal to 0.
        # The "ignore" label will be included in this based on logic that skips
        # counting those instances/regions.
        valid_classes = tf.not_equal(self.total_tp + self.total_fn + self.total_fp,
                                     0)

        # Compute averages over classes.
        qualities = tf.stack(
            [pq, sq, rq, self.total_tp, self.total_fn, self.total_fp], axis=0)
        summarized_qualities = tf.math.reduce_mean(
            tf.boolean_mask(qualities, valid_classes, axis=1), axis=1)

        return summarized_qualities

    def reset_states(self) -> None:
        """See base class."""
        tf.keras.backend.set_value(self.total_iou, np.zeros(self.num_classes))
        tf.keras.backend.set_value(self.total_tp, np.zeros(self.num_classes))
        tf.keras.backend.set_value(self.total_fn, np.zeros(self.num_classes))
        tf.keras.backend.set_value(self.total_fp, np.zeros(self.num_classes))

    def get_config(self) -> Mapping[str, Any]:
        """See base class."""
        config = {
            'num_classes': self.num_classes,
            'ignored_label': self.ignored_label,
            'max_instances_per_category': self.max_instances_per_category,
            'offset': self.offset,
        }
        base_config = super(PanopticQuality, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

